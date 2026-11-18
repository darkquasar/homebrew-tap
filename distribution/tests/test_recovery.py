"""CAS races, ordering, crash recovery and channel independence through public interfaces."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from unittest.mock import patch

from distribution import controller
from distribution.contracts import CONTROL_REPOSITORY, canonical_digest
from distribution.state import encode, file_digest
from distribution.validation import render
from test_controller import Credentials, EvidenceGitHub, LocalGit, native_success
from test_bottles import archive, checked, members, recorded_snapshot
from test_renderers import fixture
from test_contracts import approve


def versioned(snapshot, version):
    value = copy.deepcopy(snapshot)
    identity = value['payload']['identity']
    identity.update(version=version,tag='v'+version)
    value['payload']['authorization']['identity_digest'] = canonical_digest(identity)
    value['snapshot_digest'] = canonical_digest(value['payload'])
    return value


class RecoveryTests(unittest.TestCase):
    def test_malformed_public_cache_is_repaired_from_verified_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            ref=controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            tip=gh.branch_tip(CONTROL_REPOSITORY,'main')
            path='.distribution/status/patronus/2.7.1.json'
            gh.cas(CONTROL_REPOSITORY,'main',tip,{path:b'{malformed cache'})
            result=controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'],**checked(gh,ref))
            self.assertEqual(result['state'],'complete')
            status=gh.status_cache(gh.registration,'2.7.1')
            self.assertEqual(status['channels']['scoop']['state'],'published')
            self.assertIn('cache_recovery',status)

    def test_ambiguous_destination_write_keeps_candidate_ref_and_retry_reconciles(self):
        from distribution.github import RefOutcomeUnknown
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            ref=controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            actual=gh.cas
            lost=True
            def cas(repo,branch,expected,changes,**kwargs):
                nonlocal lost
                commit=actual(repo,branch,expected,changes,**kwargs)
                if repo == 'darkquasar/scoop-bucket' and commit and lost:
                    lost=False
                    raise RefOutcomeUnknown(repo,branch,commit)
                return commit
            gh.cas=cas
            first=controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'],**checked(gh,ref))
            observed=first['channels']['scoop']
            self.assertEqual(observed['state'],'unknown')
            tip=gh.branch_tip('darkquasar/scoop-bucket','main')
            self.assertEqual(observed['refs']['candidate_commit'],tip)
            second=controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'],**checked(gh,ref))
            self.assertEqual(second['channels']['scoop']['state'],'published')
            self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'),tip)

    def test_one_denied_catalogue_installation_does_not_block_another_channel(self):
        class PerRepositoryCredentials(Credentials):
            def acquire(self,role,admission,*,repository=None):
                if repository == 'darkquasar/scoop-bucket':
                    raise PermissionError('invented denied installation')
                return super().acquire(role,admission,repository=repository)
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            ref=controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            with patch('distribution.validation.subprocess.run',side_effect=native_success):
                result=controller.publish(gh.registration,ref,gh,PerRepositoryCredentials(),channels=['brew','scoop'],**checked(gh,ref))
            self.assertEqual(result['channels']['brew']['state'],'published')
            self.assertEqual(result['channels']['scoop']['state'],'failed')
            self.assertNotIn('bucket/patronus.json',gh.tree('darkquasar/scoop-bucket',gh.branch_tip('darkquasar/scoop-bucket','main')))

    def test_external_serializer_failure_keeps_other_channel_delivery_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            ref=controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            inputs=checked(gh,ref)
            with patch('yaml.safe_dump',side_effect=ValueError('invented serializer failure')):
                result=controller.publish(gh.registration,ref,gh,Credentials(),channels=['winget','scoop'],**inputs)
            self.assertEqual(result['state'],'partial-failure')
            self.assertEqual(result['channels']['winget']['failure_code'],'validation-failed')
            self.assertEqual(result['channels']['scoop']['state'],'published')

    def test_valid_prior_bytes_from_an_older_renderer_do_not_deadlock_new_release(self):
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        newer=versioned(snap,'2.8.0')
        with tempfile.TemporaryDirectory() as directory:
            gh=LocalGit(directory)
            self.records(gh,[snap,newer])
            candidate=render('scoop',reg,snap)
            path=next(iter(candidate))
            # Actual old bytes: identical JSON data, a different historical serialization.
            candidate[path]=json.dumps(json.loads(candidate[path]),separators=(',',':')).encode()
            marker=dict(schema_version=1,app='patronus',version='2.7.1',snapshot_digest=snap['snapshot_digest'],
                configuration_digest=reg['configuration_digest'],definition_digest=file_digest(candidate))
            gh.init('darkquasar/scoop-bucket',files={**candidate,'.distribution/channels/patronus.json':encode(marker)})
            result=controller.apply_channel('scoop',reg,newer,render('scoop',reg,newer),gh)
            self.assertEqual(result['state'],'published')
            tree=gh.tree('darkquasar/scoop-bucket',gh.branch_tip('darkquasar/scoop-bucket','main'))
            self.assertEqual(json.loads(tree[path]['content'])['version'],'2.8.0')

    def test_owner_metadata_retry_preserves_snapshot_and_records_new_configuration_digest(self):
        from distribution.contracts import OWNER_ID
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            original_registration=copy.deepcopy(gh.registration)
            ref=controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            record_path='.distribution/releases/patronus/2.7.1.json'
            original_record=gh.file(CONTROL_REPOSITORY,ref['records_commit'],record_path)['content']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            first=controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'],**checked(gh,ref))
            corrected=copy.deepcopy(gh.registration)
            corrected['configuration']['description']='New approved package metadata'
            gh.registration=approve(corrected)
            gh.registrations=[gh.registration]
            gh.control_tip='f'*40
            gh.context.update(mode='retry',role='authority',sha='f'*40)
            gh.env='package-authority'
            gh.controller_run.update(head_sha='f'*40,actor_id=OWNER_ID,triggering_actor_id=OWNER_ID)
            gh.list_registrations=lambda repository,commit: [copy.deepcopy(gh.registration)] if commit == 'f'*40 else []
            gh.registration_revision=lambda app,digest,commit: {'commit':'c'*40,'registration':copy.deepcopy(original_registration)}
            retry_ref=controller.authority(gh.registration,{'version':'2.7.1'},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            second=controller.publish(gh.registration,retry_ref,gh,Credentials(),channels=['scoop'],**checked(gh,retry_ref))
            self.assertEqual(second['channels']['scoop']['state'],'published')
            self.assertEqual(second['channels']['scoop']['configuration_digest'],gh.registration['configuration_digest'])
            self.assertNotEqual(first['channels']['scoop']['definition_digest'],second['channels']['scoop']['definition_digest'])
            current=gh.branch_tip(CONTROL_REPOSITORY,'distribution-records')
            self.assertEqual(gh.file(CONTROL_REPOSITORY,current,record_path)['content'],original_record)

    def test_candidate_mutation_after_validation_is_denied_before_cas(self):
        reg,snap = fixture('patronus','registration'),fixture('patronus','snapshot')
        candidate = render('scoop',reg,snap)
        with tempfile.TemporaryDirectory() as directory:
            gh = LocalGit(directory)
            repo = 'darkquasar/scoop-bucket'
            tip = gh.init(repo)
            def branch(repository):
                candidate['bucket/patronus.json'] += b'changed-after-validation'
                return 'main'
            gh.default_branch = branch
            with self.assertRaises(ValueError):
                controller.apply_channel('scoop',reg,snap,candidate,gh)
            self.assertEqual(gh.branch_tip(repo,'main'),tip)

    def records(self, gh, snapshots):
        repo = CONTROL_REPOSITORY
        initial = gh.init(repo)
        files = {f".distribution/releases/{s['payload']['identity']['app']}/{s['payload']['identity']['version']}.json":encode(s) for s in snapshots}
        gh.cas(repo,'distribution-records',None,files,base=initial)

    def test_three_simultaneous_deliveries_preserve_both_apps_and_numeric_stable_order(self):
        reg = fixture('patronus','registration')
        other = fixture('lumen','registration')
        base = fixture('patronus','snapshot')
        snapshots = [versioned(base,v) for v in ['2.8.0','2.9.0','2.10.0']]
        lumen = fixture('lumen','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh = LocalGit(directory)
            self.records(gh,snapshots+[lumen])
            repo = 'darkquasar/scoop-bucket'
            gh.init(repo,files={'sentinel.txt':b'preserve\n'})
            barrier = threading.Barrier(3)
            def deliver(pair):
                r,s = pair
                barrier.wait(timeout=10)
                return controller.apply_channel('scoop',r,s,render('scoop',r,s),gh)
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(deliver,[(reg,snapshots[1]),(other,lumen),(reg,snapshots[2])]))
            self.assertTrue(all(r['state'] in {'published','skipped'} for r in results),results)
            tree = gh.tree(repo,gh.branch_tip(repo,'main'))
            self.assertEqual(json.loads(tree['bucket/patronus.json']['content'])['version'],'2.10.0')
            self.assertEqual(json.loads(tree['bucket/lumen.json']['content'])['version'],'1.4.2')
            self.assertEqual(tree['sentinel.txt']['content'],b'preserve\n')
            tip = gh.branch_tip(repo,'main')
            older = controller.apply_channel('scoop',reg,snapshots[0],render('scoop',reg,snapshots[0]),gh)
            self.assertEqual(older['state'],'skipped')
            self.assertEqual(gh.branch_tip(repo,'main'),tip)

    def test_five_cas_conflicts_fail_visibly_and_preserve_competing_writes(self):
        reg,snap = fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh = LocalGit(directory)
            repo = 'darkquasar/scoop-bucket'
            gh.init(repo)
            conflicts = []
            def race(repository,branch,tip):
                conflicts.append(tip)
                gh.cas(repository,branch,tip,{f'other-app-{len(conflicts)}.json':b'sentinel'})
                gh.before_cas = race
            gh.before_cas = race
            result = controller.apply_channel('scoop',reg,snap,render('scoop',reg,snap),gh)
            self.assertEqual(result['failure_code'],'cas-exhausted')
            self.assertEqual(len(conflicts),5)
            tree = gh.tree(repo,gh.branch_tip(repo,'main'))
            self.assertNotIn('bucket/patronus.json',tree)
            self.assertTrue(all(f'other-app-{i}.json' in tree for i in range(1,6)))

    def test_destination_denial_and_lost_status_keep_success_and_requested_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            ref = controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            gh.denied.add('darkquasar/scoop-bucket')
            with patch('distribution.validation.subprocess.run',side_effect=native_success):
                first = controller.publish(gh.registration,ref,gh,Credentials(),channels=['brew','scoop'],**checked(gh,ref))
            self.assertEqual(first['state'],'partial-failure')
            self.assertEqual(first['channels']['brew']['state'],'published')
            self.assertEqual(first['channels']['scoop']['state'],'failed')
            formula = gh.tree(CONTROL_REPOSITORY,gh.branch_tip(CONTROL_REPOSITORY,'main'))['Formula/patronus.rb']['content']
            gh.denied.clear()
            gh.denied.add(CONTROL_REPOSITORY)  # Crash/storage loss after destination push.
            second = controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'],**checked(gh,ref))
            self.assertEqual(second['state'],'partial-failure')
            self.assertEqual(second['channels']['scoop']['state'],'published')
            tip = gh.branch_tip('darkquasar/scoop-bucket','main')
            gh.denied.clear()
            third = controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'],**checked(gh,ref))
            self.assertEqual(third['state'],'complete')
            self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'),tip)
            tree = gh.tree(CONTROL_REPOSITORY,gh.branch_tip(CONTROL_REPOSITORY,'main'))
            self.assertEqual(tree['Formula/patronus.rb']['content'],formula)
            status = json.loads(tree['.distribution/status/patronus/2.7.1.json']['content'])
            self.assertEqual(status['channels']['brew']['state'],'published')
            self.assertEqual(status['channels']['scoop']['state'],'published')

    def test_forged_marker_cannot_invent_newer_authorized_release(self):
        reg, snap = fixture('patronus','registration'), fixture('patronus','snapshot')
        newer = versioned(snap,'99.0.0')
        definition = render('scoop',reg,newer)
        marker = dict(schema_version=1,app='patronus',version='99.0.0',snapshot_digest=newer['snapshot_digest'],
            definition_digest=file_digest(definition),configuration_digest=reg['configuration_digest'])
        with tempfile.TemporaryDirectory() as directory:
            gh = LocalGit(directory)
            gh.init('darkquasar/scoop-bucket',files={**definition,'.distribution/channels/patronus.json':encode(marker)})
            tip = gh.branch_tip('darkquasar/scoop-bucket','main')
            with self.assertRaises(ValueError):
                controller.apply_channel('scoop',reg,snap,render('scoop',reg,snap),gh)
            self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'),tip)


def publisher(directory):
    gh = EvidenceGitHub(directory)
    ref = controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
    gh.context['role'] = 'publish'
    gh.env = 'package-publisher'
    gh.init('darkquasar/scoop-bucket')
    gh._service()
    return gh, ref


def second_run(gh, directory, run_id):
    """Another publisher run for the same app/version sharing the same external services."""
    from pathlib import Path
    other = copy.copy(gh)
    other.root = Path(directory)/f'run-{run_id}'
    other.root.mkdir()
    other.context = dict(gh.context, run_id=run_id)
    other.controller_run = dict(gh.controller_run, id=run_id)
    return other


def run_publish(gh, ref, inputs, channels=('brew',)):
    return controller.publish(gh.registration,ref,gh,Credentials(),channels=list(channels),**inputs)


class BottleRaceTests(unittest.TestCase):
    def test_two_publishers_one_version_one_release_and_the_loser_retries_onto_the_winner(self):
        from distribution.bottles import select_bottles
        with tempfile.TemporaryDirectory() as directory, patch(
                'distribution.validation.subprocess.run',side_effect=native_success):
            a, ref = publisher(directory)
            b = second_run(a, directory, 41)
            inputs_a, inputs_b = checked(a,ref,salt='run-40'), checked(b,ref,salt='run-41')
            self.assertFalse(set(inputs_a['archives'].values()) & set(inputs_b['archives'].values()))
            service = a.releases_service
            paused, go = threading.Event(), threading.Event()
            def pause(run_id, release_id):
                if run_id == 40 and not go.is_set():
                    paused.set()
                    self.assertTrue(go.wait(30))
            service.before_publish = pause
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(run_publish, a, ref, inputs_a)
                self.assertTrue(paused.wait(30))
                self.assertEqual(service.ops('draft-read', run=40)[-1][1], 'draft-read')
                won = run_publish(b, ref, inputs_b)
                go.set()
                lost = pending.result(timeout=60)
            self.assertEqual(won['channels']['brew']['state'],'published')
            self.assertEqual(lost['channels']['brew']['state'],'failed')
            self.assertEqual(lost['channels']['brew']['failure_code'],'bottle-race')
            published = [r for r in service.releases.values() if not r['draft']]
            self.assertEqual(len(published),1)
            self.assertEqual(published[0]['assets'],inputs_b['archives'])
            winner = published[0]['id']
            self.assertEqual(published[0]['creator'],41)
            self.assertTrue({e[1] for e in service.log if e[0] == 40 and e[2] == winner} <= {'read','download'})
            own = {e[2] for e in service.ops('create',run=40)}
            self.assertEqual(len(own),1)
            self.assertFalse(own & set(service.releases))
            self.assertFalse([e for e in service.ops('cas',run=40) if 'Formula/patronus.rb' in e[3]])
            snap = recorded_snapshot(a,ref)
            formula = render('brew',a.registration,snap,inputs_b['evidence']['brew']['bottle_set'])
            tree = a.tree(CONTROL_REPOSITORY,a.branch_tip(CONTROL_REPOSITORY,'main'))
            self.assertEqual(tree['Formula/patronus.rb']['content'],formula['Formula/patronus.rb'])
            selected = select_bottles(a.registration,snap,a)
            self.assertEqual(selected['bottle_set'],inputs_b['evidence']['brew']['bottle_set'])
            before = len(service.log)
            retry = run_publish(a, ref, checked(a,ref,selected=selected))
            self.assertEqual(retry['channels']['brew']['state'],'published')
            self.assertEqual(retry['channels']['brew']['bottle_set_sha256'],
                             canonical_digest(inputs_b['evidence']['brew']['bottle_set']))
            self.assertFalse([e for e in service.log[before:] if e[1] in {'create','upload','publish','delete'}])
            self.assertEqual(len([r for r in service.releases.values() if not r['draft']]),1)


class BottleEvidenceBindingTests(unittest.TestCase):
    """Anything substituted after the native checks blocks every release and formula write."""
    def assert_blocked(self, gh, output):
        brew = output['channels']['brew']
        self.assertEqual(brew['state'],'failed')
        self.assertEqual(brew['failure_code'],'native-check-failed')
        self.assertFalse(gh.releases_service.ops('create','upload','publish','delete'))
        self.assertNotIn('Formula/patronus.rb',gh.tree(CONTROL_REPOSITORY,gh.branch_tip(CONTROL_REPOSITORY,'main')))

    def test_substituted_archive_tag_hash_or_formula_field_blocks_the_formula_write(self):
        import hashlib
        for change in ['archive','tag-hash','formula-identity','result-binding']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, patch(
                    'distribution.validation.subprocess.run',side_effect=native_success):
                gh, ref = publisher(directory)
                inputs = checked(gh,ref)
                rebuilt = checked(gh,ref,salt='substitute')['archives']
                name = 'patronus-2.7.1.x86_64_linux.bottle.tar.gz'
                brew = inputs['evidence']['brew']
                if change == 'archive':
                    inputs['archives'][name] = rebuilt[name]
                elif change == 'tag-hash':
                    inputs['archives'][name] = rebuilt[name]
                    brew['bottle_set']['tags']['x86_64_linux'] = hashlib.sha256(rebuilt[name]).hexdigest()
                elif change == 'formula-identity':
                    brew['identity']['formula_sha256'] = 'f'*64
                    for entry in inputs['evidence']['results']:
                        if entry['manager'] == 'brew':
                            entry['binding'] = 'f'*64
                else:
                    entry = next(e for e in inputs['evidence']['results'] if e['platform'] == 'linux-amd64')
                    entry['binding'] = 'e'*64
                output = run_publish(gh,ref,inputs,channels=('brew','scoop'))
                self.assert_blocked(gh,output)
                self.assertEqual(output['channels']['scoop']['state'],'published')

    def test_changed_registration_after_checks_blocks_bottle_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            gh, ref = publisher(directory)
            inputs = checked(gh,ref)
            snap = recorded_snapshot(gh,ref)
            corrected = copy.deepcopy(gh.registration)
            corrected['configuration']['description'] = 'Changed after the native checks'
            corrected = approve(corrected)
            result = controller.apply_bottles(corrected,snap,inputs['evidence']['brew']['bottle_set'],
                                              inputs['archives'],inputs['evidence'],gh)
            self.assertEqual(result['state'],'failed')
            self.assertEqual(result['failure_code'],'native-check-failed')
            self.assertFalse(gh.releases_service.ops('create','upload','publish'))


class NativeGateTests(unittest.TestCase):
    """Scoop/WinGet publish or prepare only definitions hosted checks passed (PD-17)."""
    def outcome(self, mutate, channels=('scoop','winget')):
        with tempfile.TemporaryDirectory() as directory:
            gh, ref = publisher(directory)
            inputs = checked(gh,ref)
            mutate(inputs)
            tip = gh.branch_tip('darkquasar/scoop-bucket','main')
            output = run_publish(gh,ref,inputs,channels=channels)
            if output['channels']['scoop']['failure_code'] == 'native-check-failed':
                self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'),tip)
            return output['channels']

    def result(self, inputs, manager, platform):
        return next(e for e in inputs['evidence']['results'] if e['manager'] == manager and e['platform'] == platform)

    def test_missing_failed_or_rebound_evidence_fails_the_channel_with_platforms(self):
        def missing(inputs):
            inputs['evidence'] = None
        def failed_amd64(inputs):
            entry = self.result(inputs,'scoop','windows-amd64')
            entry.update(outcome='failed',reason='version mismatch',steps=dict(entry['steps'],version='failed'))
        def other_candidate(inputs):
            inputs['evidence']['candidates']['scoop'] = 'f'*64
            for e in inputs['evidence']['results']:
                if e['manager'] == 'scoop':
                    e['binding'] = 'f'*64
        def rebound_result(inputs):
            self.result(inputs,'winget','windows-arm64')['binding'] = 'f'*64
        def missing_arm64(inputs):
            inputs['evidence']['results'].remove(self.result(inputs,'scoop','windows-arm64'))
        def wrong_static_reason(inputs):
            entry = self.result(inputs,'scoop','windows-arm64')
            entry.update(outcome='statically-mapped',reason='runner-busy',
                         steps={k:'not-run' for k in entry['steps']})
        def static_amd64(inputs):
            entry = self.result(inputs,'scoop','windows-amd64')
            entry.update(outcome='statically-mapped',reason='no-hosted-runner',
                         steps={k:'not-run' for k in entry['steps']})
        cases = [(missing,{'scoop','winget'},'native-checks.json missing'),
                 (failed_amd64,{'scoop'},'windows-amd64'),
                 (other_candidate,{'scoop'},'different candidate digest'),
                 (rebound_result,{'winget'},'windows-arm64'),
                 (missing_arm64,{'scoop'},'windows-arm64: missing'),
                 (wrong_static_reason,{'scoop'},'windows-arm64'),
                 (static_amd64,{'scoop'},'windows-amd64')]
        for mutate, failing, text in cases:
            with self.subTest(case=mutate.__name__):
                channels = self.outcome(mutate)
                for channel in ['scoop','winget']:
                    native = channels[channel]['failure_code'] == 'native-check-failed'
                    self.assertEqual(native, channel in failing, (channel, channels[channel]))
                    if native:
                        self.assertIn(text,' '.join(channels[channel]['failing_platforms']))
                        self.assertIn(text,channels[channel]['message'])

    def test_arm64_statically_mapped_for_no_hosted_runner_is_accepted(self):
        def static(inputs):
            for manager in ['scoop','winget']:
                entry = self.result(inputs,manager,'windows-arm64')
                entry.update(outcome='statically-mapped',reason='no-hosted-runner',
                             steps={k:'not-run' for k in entry['steps']})
        channels = self.outcome(static)
        self.assertEqual(channels['scoop']['state'],'published')
        self.assertNotEqual(channels['winget']['failure_code'],'native-check-failed')
        self.assertIn('native_checks',channels['scoop'])

    def test_malformed_foreign_or_other_run_evidence_fails_every_channel(self):
        def unknown_field(inputs):
            inputs['evidence']['extra'] = True
        def unknown_result_field(inputs):
            inputs['evidence']['results'][0]['note'] = 'x'
        def other_run(inputs):
            inputs['evidence']['run'] = dict(id=39,attempt=1,
                url='https://github.com/darkquasar/homebrew-tap/actions/runs/39/attempts/1')
        def other_snapshot(inputs):
            inputs['evidence']['snapshot_digest'] = 'f'*64
        def passed_with_failed_step(inputs):
            self.result(inputs,'scoop','windows-amd64')['steps']['uninstall'] = 'failed'
        for mutate in [unknown_field,unknown_result_field,other_run,other_snapshot,passed_with_failed_step]:
            with self.subTest(case=mutate.__name__):
                channels = self.outcome(mutate,channels=('brew','scoop','winget'))
                for channel in ['brew','scoop','winget']:
                    self.assertEqual(channels[channel]['failure_code'],'native-check-failed',(channel,channels[channel]))
