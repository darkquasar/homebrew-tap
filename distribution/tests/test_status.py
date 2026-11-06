"""WinGet exact-version observation and submission preservation."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from distribution import controller
from distribution.validation import render
from test_renderers import fixture
from test_controller import LocalGit
from test_contracts import approve
from test_controller import Credentials, EvidenceGitHub
from test_recovery import versioned
from distribution.state import encode
from distribution.contracts import CONTROL_REPOSITORY


class Source:
    def __init__(self, versions=(), unavailable=False):
        self.versions = list(versions)
        self.unavailable = unavailable

    def observe(self, registration, version):
        if self.unavailable:
            raise ValueError('source-unavailable')
        return dict(versions=self.versions, package_id=registration['configuration']['identifiers']['winget'],
            source='winget', runner_os='Windows', verified=True, run_id=80, attempt=1)


class WingetGit(LocalGit):
    def __init__(self, root):
        super().__init__(root)
        self.upstream_sha = self.init('microsoft/winget-pkgs', 'master', {'README.md':b'upstream\n'})
        upstream_remote = self.remotes['microsoft/winget-pkgs'][0]
        fork_remote = Path(root)/'fork.git'
        subprocess.run(['git','clone','--bare',str(upstream_remote),str(fork_remote)],check=True,capture_output=True,timeout=30)
        self.remotes['darkquasar/winget-pkgs'] = (fork_remote,'winget-catalogue-docs')
        self.cas('darkquasar/winget-pkgs','winget-catalogue-docs',None,{'README.md':b'fork docs must stay local\n'},base=self.upstream_sha)
        self.cas('darkquasar/winget-pkgs','master',self.upstream_sha,{'unrelated.txt':b'fork mirror edit\n'})
        self.prs = []
        self.pr_error = False
        self.source = Source()
        self.context = {'mode':'prepare'}
        self.statuses = {}

    def upstream(self):
        return {'repository':'microsoft/winget-pkgs','branch':'master','sha':self.upstream_sha}

    def pull_requests(self, registration, version):
        if self.pr_error:
            raise ValueError('GitHub PR lookup unavailable')
        return copy.deepcopy(self.prs)

    def status_cache(self, registration, version):
        return copy.deepcopy(self.statuses.get(version,{}))

    def controller_context(self):
        return self.context

    def merge_base(self, upstream, repository, head):
        return self.git(self.remotes[repository][0], 'merge-base', upstream, head).decode().strip()

    def diff_paths(self, repository, base, head):
        return set(self.git(self.remotes[repository][0],'diff','--name-only',base,head).decode().splitlines())

    def commit_available(self, repository, commit):
        return bool(self.git(self.remotes[repository][0],'cat-file','-e',commit,check=False) == b'')


class WingetTests(unittest.TestCase):
    def test_unreadable_status_cache_does_not_replace_live_pr_lookup_or_block_preparation(self):
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh=WingetGit(directory)
            def unavailable(*args):
                raise ValueError('corrupt publisher-writable cache')
            gh.status_cache=unavailable
            result=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(result['state'],'prepared')
            self.assertIn('cache_warning',result['refs'])
            gh.pr_error=True
            unknown=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(unknown['state'],'unknown')

    def test_transient_prerequisite_read_does_not_instruct_owner_to_sync_fork(self):
        from distribution.github import APIError
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh=WingetGit(directory)
            controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            def unavailable(*args):
                raise APIError(503,'GET /repos/darkquasar/winget-pkgs/compare/base...head')
            gh.merge_base=unavailable
            outcome=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(outcome['failure_code'],'destination-unavailable')
            self.assertNotIn('gh repo sync',outcome['message'])

    def test_owner_closed_retry_remains_prepared_on_status_and_new_pr_has_its_own_submitted_head(self):
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh=WingetGit(directory)
            initial=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            old=initial['destination_commit']
            gh.statuses['2.7.1']={'channels':{'winget':initial}}
            pr=dict(number=1,url='https://github.com/microsoft/winget-pkgs/pull/1',state='closed',merged=False,
                head_sha=old,head_repository='darkquasar/winget-pkgs',head_branch='patronus/2.7.1',feedback='correct metadata')
            gh.prs=[pr]
            changed=copy.deepcopy(reg)
            changed['configuration']['description']='Owner correction after rejection'
            changed=approve(changed)
            gh.context['mode']='retry'
            corrected=controller.apply_channel('winget',changed,snap,render('winget',changed,snap),gh)
            gh.statuses['2.7.1']={'channels':{'winget':corrected}}
            observed=controller.winget_status(changed,snap,gh)
            self.assertEqual(observed['state'],'prepared')
            self.assertFalse(observed['refs']['externally_edited'])
            self.assertEqual(observed['refs']['pull_requests'][0]['number'],1)
            gh.statuses['2.7.1']={'channels':{'winget':observed}}
            new=corrected['destination_commit']
            gh.prs=[pr,{**pr,'number':2,'state':'open','head_sha':new,
                'url':'https://github.com/microsoft/winget-pkgs/pull/2'}]
            submitted=controller.winget_status(changed,snap,gh)
            self.assertEqual(submitted['state'],'submitted')
            self.assertEqual(submitted['refs']['submitted_head'],new)
            self.assertFalse(submitted['refs']['externally_edited'])

    def test_merged_indexed_submission_stays_published_when_fork_objects_are_gone(self):
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh=WingetGit(directory)
            gh.source=Source(['2.7.1'])
            gh.prs=[dict(number=1,url='https://github.com/microsoft/winget-pkgs/pull/1',state='closed',merged=True,
                head_sha='b'*40,head_repository='darkquasar/winget-pkgs',head_branch='patronus/2.7.1',feedback='')]
            observed=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(observed['state'],'published')
            self.assertIsNone(gh.branch_tip('darkquasar/winget-pkgs','patronus/2.7.1'))

    def test_closed_retry_preserves_feedback_and_open_external_edits_are_never_reset(self):
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh=WingetGit(directory)
            prepared=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            gh.statuses['2.7.1']={'channels':{'winget':prepared}}
            head=prepared['destination_commit']
            pr=dict(number=1,url='https://github.com/microsoft/winget-pkgs/pull/1',state='open',merged=False,
                head_sha=head,head_repository='darkquasar/winget-pkgs',head_branch='patronus/2.7.1',feedback='correct description')
            gh.prs=[pr]
            changed=copy.deepcopy(reg)
            changed['configuration']['description']='Reviewed corrected description'
            changed=approve(changed)
            new_candidate=render('winget',changed,snap)
            moved=gh.cas('darkquasar/winget-pkgs','patronus/2.7.1',head,new_candidate)
            observed=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(observed['state'],'submitted')
            self.assertTrue(observed['refs']['externally_edited'])
            self.assertEqual(observed['refs']['submitted_head'],head)
            self.assertEqual(observed['refs']['current_head'],moved)
            self.assertEqual(gh.branch_tip('darkquasar/winget-pkgs','patronus/2.7.1'),moved)
            gh.statuses['2.7.1']={'channels':{'winget':observed}}
            gh.prs=[{**pr,'state':'closed','head_sha':moved}]
            denied=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(denied['state'],'rejected')
            gh.context['mode']='retry'
            corrected=controller.apply_channel('winget',changed,snap,new_candidate,gh)
            self.assertEqual(corrected['state'],'prepared')
            self.assertEqual(corrected['refs']['pull_requests'][0]['number'],1)
            self.assertIn('correct description',corrected['refs']['pull_requests'][0]['feedback'])
            self.assertEqual(corrected['refs'].get('previous_definition_digest'),prepared['definition_digest'])

    def test_out_of_order_version_branches_survive_and_missing_fork_base_reports_recovery(self):
        reg,snap=fixture('patronus','registration'),fixture('patronus','snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh=WingetGit(directory)
            newer=versioned(snap,'2.10.0')
            first=controller.apply_channel('winget',reg,newer,render('winget',reg,newer),gh)
            older=controller.apply_channel('winget',reg,snap,render('winget',reg,snap),gh)
            self.assertEqual(first['state'],'prepared')
            self.assertEqual(older['state'],'prepared')
            for value in [newer,snap]:
                version=value['payload']['identity']['version']
                head=gh.branch_tip('darkquasar/winget-pkgs','patronus/'+version)
                tree=gh.tree('darkquasar/winget-pkgs',head)
                self.assertTrue(set(render('winget',reg,value)) <= set(tree))
            gh.commit_available=lambda repository,commit: False
            result=controller.apply_channel('winget',reg,versioned(snap,'2.11.0'),render('winget',reg,versioned(snap,'2.11.0')),gh)
            self.assertEqual(result['failure_code'],'fork-sync-required')
            self.assertIn('--branch master --source microsoft/winget-pkgs',result['message'])
            self.assertIsNone(gh.branch_tip('darkquasar/winget-pkgs','patronus/2.11.0'))

    def test_next_release_refreshes_prior_unresolved_submission_without_repreparing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            ref = controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            tip = gh.branch_tip(CONTROL_REPOSITORY,'distribution-records')
            current = json.loads(gh.file(CONTROL_REPOSITORY,tip,'.distribution/releases/patronus/2.7.1.json')['content'])
            older = versioned(current,'2.6.0')
            gh.cas(CONTROL_REPOSITORY,'distribution-records',tip,{'.distribution/releases/patronus/2.6.0.json':encode(older)})
            status_path = '.distribution/status/patronus/2.6.0.json'
            tip = gh.branch_tip(CONTROL_REPOSITORY,'main')
            gh.cas(CONTROL_REPOSITORY,'main',tip,{status_path:encode({'channels':{'winget':{'state':'submitted','refs':{}}}})})
            winget_dir = Path(directory)/'winget'
            winget_dir.mkdir()
            service = WingetGit(winget_dir)
            service.prs = [dict(number=2,url='https://github.com/microsoft/winget-pkgs/pull/2',state='closed',
                merged=True,head_sha=service.upstream_sha,head_repository='darkquasar/winget-pkgs',
                head_branch='patronus/2.6.0',feedback='')]
            gh.remotes.update(service.remotes)
            gh.source=Source([])
            gh.pull_requests=service.pull_requests
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'])
            saved = gh.status_cache(gh.registration,'2.6.0')
            self.assertEqual(saved['channels']['winget']['state'],'accepted-awaiting-indexing')
            self.assertEqual(saved['channels']['winget']['refs']['pull_requests'][0]['number'],2)

    def test_owner_status_observes_actual_native_package_without_republishing(self):
        from distribution.contracts import OWNER_ID
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            ref = controller.authority(gh.registration,{'run_id':50,'attempt':1},gh,Credentials())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            controller.publish(gh.registration,ref,gh,Credentials(),channels=['scoop'])
            tip = gh.branch_tip('darkquasar/scoop-bucket','main')
            gh.context['mode']='status'
            gh.controller_run.update(actor_id=OWNER_ID,triggering_actor_id=OWNER_ID)
            gh.source=Source(unavailable=True)
            gh.pull_requests=lambda registration,version: []
            credentials=Credentials()
            result=controller.status(gh.registration,'2.7.1',gh,credentials)
            self.assertEqual(result['channels']['scoop']['state'],'published')
            self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'),tip)
            self.assertEqual(credentials.minted,['publish'])

    def test_open_submission_keeps_submitted_digest_and_bytes_on_metadata_correction(self):
        reg,snap = fixture('patronus','registration'),fixture('patronus','snapshot')
        candidate = render('winget',reg,snap)
        with tempfile.TemporaryDirectory() as directory:
            gh = WingetGit(directory)
            prepared = controller.apply_channel('winget',reg,snap,candidate,gh)
            gh.statuses['2.7.1'] = {'channels':{'winget':prepared}}
            head = prepared['destination_commit']
            gh.prs = [dict(number=1,url='https://github.com/microsoft/winget-pkgs/pull/1',state='open',
                merged=False,head_sha=head,head_repository='darkquasar/winget-pkgs',head_branch='patronus/2.7.1',feedback='')]
            changed = copy.deepcopy(reg)
            changed['configuration']['description']='Owner-approved corrected metadata'
            changed = approve(changed)
            observed = controller.apply_channel('winget',changed,snap,render('winget',changed,snap),gh)
            self.assertEqual(observed['state'],'submitted')
            self.assertEqual(gh.branch_tip('darkquasar/winget-pkgs','patronus/2.7.1'),head)
            self.assertEqual(observed['definition_digest'],prepared['definition_digest'])
            self.assertIsNone(observed['validation'])

    def test_version_branch_uses_exact_microsoft_base_and_only_version_manifests(self):
        reg, snap = fixture('patronus','registration'), fixture('patronus','snapshot')
        candidate = render('winget',reg,snap)
        with tempfile.TemporaryDirectory() as directory:
            gh = WingetGit(directory)
            result = controller.apply_channel('winget',reg,snap,candidate,gh)
            self.assertEqual(result['state'],'prepared')
            head = gh.branch_tip('darkquasar/winget-pkgs','patronus/2.7.1')
            self.assertEqual(result['refs']['upstream_base'],gh.upstream_sha)
            self.assertEqual(gh.diff_paths('darkquasar/winget-pkgs',gh.upstream_sha,head),set(candidate))
            self.assertNotIn('unrelated.txt',gh.tree('darkquasar/winget-pkgs',head))
            repeated = controller.apply_channel('winget',reg,snap,candidate,gh)
            self.assertEqual(repeated['destination_commit'],head)

    def test_observation_distinguishes_open_merged_absent_visible_closed_and_unreadable(self):
        reg = fixture('patronus','registration')
        with tempfile.TemporaryDirectory() as directory:
            gh = WingetGit(directory)
            pr = dict(number=1,url='https://github.com/microsoft/winget-pkgs/pull/1',
                state='open',merged=False,head_sha='b'*40,head_repository='darkquasar/winget-pkgs',
                head_branch='patronus/2.7.1',feedback='feedback')
            for state, merged, versions, expected in [('open',False,[],'submitted'),
                    ('closed',True,[],'accepted-awaiting-indexing'),
                    ('closed',True,['2.7.1'],'published'),('closed',False,[],'rejected')]:
                gh.prs = [{**pr,'state':state,'merged':merged}]
                observed = controller.observe_winget(reg,'2.7.1',gh,Source(versions))
                self.assertEqual(observed['state'],expected)
                self.assertEqual(observed['refs']['pull_requests'][0]['url'],pr['url'])
            gh.pr_error = True
            observed = controller.observe_winget(reg,'2.7.1',gh,Source())
            self.assertEqual(observed['state'],'unknown')
