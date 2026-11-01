import copy
import hashlib
import unittest

from distribution import contracts, snapshot, trust
from test_contracts import approve, registration
from test_trust import GitHub, CONTROL, RECORDS


def baseline_approval(registration, identity):
    return dict(owner_id=contracts.OWNER_ID,triggering_actor_id=contracts.OWNER_ID,
        mode='approve-baseline',role='authority',controller_sha=CONTROL,
        configuration_digest=registration['configuration_digest'],
        identity_digest=contracts.canonical_digest(identity),reference='fixture:owner-baseline/40/1')


class BaselineTests(unittest.TestCase):
    def test_baseline_requires_owner_approval_of_exact_inspected_identity(self):
        github = GitHub()
        identity = snapshot.inspect_identity(github.registration,'v2.7.1',github)
        approval = baseline_approval(github.registration,identity)
        for key,value in [('owner_id',123),('triggering_actor_id',123),
            ('identity_digest','0'*64),('configuration_digest','0'*64),
            ('mode','prepare'),('role','publish'),('reference','')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                snapshot.approve_snapshot(github.registration,identity,{**approval,key:value})

    def test_baseline_is_deterministic_and_does_not_invent_release_history(self):
        github = GitHub('lumen','1.4.2')
        identity = snapshot.inspect_identity(github.registration,'v1.4.2',github)
        approval = baseline_approval(github.registration,identity)
        approved = snapshot.approve_snapshot(github.registration,identity,approval)
        self.assertEqual(approved,snapshot.approve_snapshot(github.registration,identity,approval))
        self.assertEqual(approved['payload']['authorization']['kind'],'baseline')
        self.assertEqual(approved['snapshot_digest'],contracts.canonical_digest(approved['payload']))
        identity['commit'] = 'f'*40
        with self.assertRaises(ValueError):
            snapshot.approve_snapshot(github.registration,identity,approval)


def prepared():
    github = GitHub()
    frozen = trust.verify_release(github.registration,github.receipt,github)
    github.record_documents = {RECORDS:copy.deepcopy(frozen)}
    github.original_registration = copy.deepcopy(github.registration)
    github.context['role'] = 'publish'
    github.controller_context = lambda: copy.deepcopy(github.context)
    github.registration_revision = lambda app,digest,tip: {
        'commit': CONTROL,'registration':copy.deepcopy(github.original_registration)}
    ref = dict(schema_version=1, app='patronus',version='2.7.1',controller_sha=CONTROL,
        configuration_digest=github.registration['configuration_digest'],
        records_commit=RECORDS,snapshot_digest=frozen['snapshot_digest'])
    return github,frozen,ref


class ProtectedSnapshotTests(unittest.TestCase):
    def test_protected_record_is_read_back_and_current_release_replacement_denied(self):
        github,frozen,ref = prepared()
        self.assertEqual(snapshot.load_authorized_snapshot(github.registration,ref,github),frozen)
        name = next(iter(github.assets))
        github.assets[name] = b'new bytes'
        github.checksums = ''.join(hashlib.sha256(data).hexdigest()+'  '+n+'\n'
                                  for n,data in github.assets.items()).encode()
        github.release_metadata['assets'] = [{'name':n,'size':len(data)} for n,data in github.assets.items()]
        github.release_metadata['assets'].append({'name':'checksums.txt','size':len(github.checksums)})
        with self.assertRaises(ValueError):
            snapshot.load_authorized_snapshot(github.registration,ref,github)

    def test_forged_stale_unreachable_or_wrong_role_handoffs_are_denied(self):
        for field,value in [('app','lumen'),('controller_sha','f'*40),
            ('configuration_digest','0'*64),('snapshot_digest','0'*64),('version','../2.7.1'),
            ('extra','token')]:
            github,frozen,ref = prepared()
            ref[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                snapshot.load_authorized_snapshot(github.registration,ref,github)
        for change in ['unreachable','authority','default-branch','stale-between-jobs','symlink','changed-record']:
            github,frozen,ref = prepared()
            if change == 'unreachable':
                github.reachable = False
            elif change == 'authority':
                github.context['role'] = 'authority'
            elif change == 'default-branch':
                github.context['ref'] = 'refs/heads/main'
            elif change == 'stale-between-jobs':
                github.control_tip = 'e'*40
            elif change == 'symlink':
                github.file_kind = 'symlink'
            else:
                github.record_documents[RECORDS]['payload']['authorization']['owner_id'] = 123
            with self.subTest(change=change), self.assertRaises(ValueError):
                snapshot.load_authorized_snapshot(github.registration,ref,github)

    def test_metadata_retry_preserves_original_snapshot_and_initial_approval(self):
        github,frozen,ref = prepared()
        updated = copy.deepcopy(github.registration)
        updated['configuration']['description'] = 'Owner-approved new description'
        updated = approve(updated)
        github.registrations = [updated]
        ref['configuration_digest'] = updated['configuration_digest']
        actual = snapshot.load_authorized_snapshot(updated,ref,github)
        self.assertEqual(actual,frozen)
        self.assertNotEqual(actual['payload']['initial_configuration_digest'],updated['configuration_digest'])
        for field,value in [('repository','darkquasar/other'),('history_branch','other'),
                            ('identifiers',{**updated['configuration']['identifiers'],'winget':'darkquasar.PATRONUS'})]:
            bad = copy.deepcopy(updated)
            bad['configuration'][field] = value
            if field == 'identifiers':
                bad['configuration']['destinations']['winget']['path'] = 'manifests/d/darkquasar/PATRONUS'
            bad = approve(bad)
            github.registrations = [bad]
            ref['configuration_digest'] = bad['configuration_digest']
            with self.subTest(field=field), self.assertRaises(ValueError):
                snapshot.load_authorized_snapshot(bad,ref,github)

    def test_record_rewrite_and_tag_retarget_fail_even_before_first_channel(self):
        for change in ['record-tip','tag','unapproved-metadata','original-config']:
            github,frozen,ref = prepared()
            if change == 'record-tip':
                github.records_tip = 'e'*40
                changed = copy.deepcopy(frozen)
                changed['payload']['authorization']['reference']['run_id'] = 500
                changed['snapshot_digest'] = contracts.canonical_digest(changed['payload'])
                github.record_documents[github.records_tip] = changed
            elif change == 'tag':
                github.source_commit = 'f'*40
            elif change == 'unapproved-metadata':
                github.registration['configuration']['description'] = 'unapproved'
            else:
                github.original_registration['configuration']['description'] = 'different original'
                github.original_registration = approve(github.original_registration)
            with self.subTest(change=change), self.assertRaises(ValueError):
                snapshot.load_authorized_snapshot(github.registration,ref,github)

    def test_baseline_load_survives_receipt_expiry_with_protected_original_authority(self):
        github,frozen,ref = prepared()
        identity = snapshot.inspect_identity(github.registration,'v2.7.1',github)
        frozen = snapshot.approve_snapshot(github.registration,identity,
                    baseline_approval(github.registration,identity))
        github.record_documents[RECORDS] = frozen
        ref['snapshot_digest'] = frozen['snapshot_digest']
        github.artifact_missing = True
        self.assertEqual(snapshot.load_authorized_snapshot(github.registration,ref,github),frozen)


    def test_owner_can_retry_frozen_release_after_approved_dispatcher_replacement(self):
        gh,frozen,ref = prepared()
        changed = copy.deepcopy(gh.registration)
        changed['configuration'].update(dispatcher_app_id=9101,dispatcher_actor_id=8101)
        changed = approve(changed)
        gh.registration=changed
        gh.registrations=[changed]
        gh.context.update(mode='retry',role='publish')
        gh.controller_run.update(actor_id=contracts.OWNER_ID,triggering_actor_id=contracts.OWNER_ID)
        ref['configuration_digest']=changed['configuration_digest']
        self.assertEqual(snapshot.load_authorized_snapshot(changed,ref,gh),frozen)
        gh.context['mode']='prepare'
        gh.controller_run.update(actor_id=8001,triggering_actor_id=8001)
        with self.assertRaises(ValueError):
            snapshot.load_authorized_snapshot(changed,ref,gh)

    def test_observation_modes_cannot_admit_publisher(self):
        for mode in ['status','inspect-baseline']:
            gh,frozen,ref=prepared()
            gh.context['mode']=mode
            gh.controller_run.update(actor_id=contracts.OWNER_ID,triggering_actor_id=contracts.OWNER_ID)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                snapshot.load_authorized_snapshot(gh.registration,ref,gh)

    def test_second_app_admission_and_protected_readback(self):
        gh=GitHub('lumen','1.4.2')
        frozen=trust.verify_release(gh.registration,gh.receipt,gh)
        gh.record_documents={RECORDS:frozen}
        gh.controller_context=lambda: {**gh.context,'role':'publish'}
        gh.registration_revision=lambda app,digest,tip: {'commit':CONTROL,'registration':gh.registration}
        ref=dict(schema_version=1,app='lumen',version='1.4.2',controller_sha=CONTROL,
            configuration_digest=gh.registration['configuration_digest'],records_commit=RECORDS,
            snapshot_digest=frozen['snapshot_digest'])
        self.assertEqual(snapshot.load_authorized_snapshot(gh.registration,ref,gh),frozen)

    def test_saved_fixtures_match_public_function_outputs(self):
        from pathlib import Path
        import json
        path=Path(__file__).parent/'fixtures'
        gh,frozen,ref=prepared()
        for name,value in [('patronus-snapshot',frozen),('patronus-receipt',gh.receipt),('patronus-record-ref',ref)]:
            self.assertEqual(json.loads((path/(name+'.json')).read_text()),value)
        gh=GitHub('lumen','1.4.2')
        identity=snapshot.inspect_identity(gh.registration,'v1.4.2',gh)
        frozen=snapshot.approve_snapshot(gh.registration,identity,baseline_approval(gh.registration,identity))
        self.assertEqual(json.loads((path/'lumen-snapshot.json').read_text()),frozen)
