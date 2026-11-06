"""Public distribution behavior with real native definitions and local Git remotes."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from distribution import controller
from distribution.contracts import canonical_digest
from distribution.state import encode
from distribution.validation import render
from test_renderers import fixture
import test_validation
import test_trust
from distribution.contracts import CONTROL_REPOSITORY, OWNER_ID

REAL_RUN = subprocess.run


def native_success(command, **kwargs):
    if command[0] == 'brew' or command[:3] == ['git', '-C', '/fixture/homebrew']:
        return test_validation.Validation.native_success(command, **kwargs)
    return REAL_RUN(command, **kwargs)

class LocalGit:
    """External Git service double: actual commits and fast-forward pushes to bare repos."""
    def __init__(self, root):
        self.root = Path(root)
        self.remotes = {}
        self.counter = 0
        self.before_cas = None
        self.denied = set()
        self.lock = __import__('threading').Lock()

    def git(self, root, *args, check=True, input=None):
        return subprocess.run(['git', '-C', str(root), *args], input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, timeout=30).stdout

    def init(self, repo, branch='main', files=None):
        remote = self.root / (repo.replace('/','-') + '.git')
        subprocess.run(['git', 'init', '--bare', '--initial-branch='+branch, str(remote)],
                       check=True, capture_output=True, timeout=30)
        work = self.root / (repo.replace('/','-') + '-seed')
        subprocess.run(['git', 'clone', str(remote), str(work)], check=True, capture_output=True, timeout=30)
        self.git(work, 'config', 'user.email', 'fixture@example.invalid')
        self.git(work, 'config', 'user.name', 'Fixture')
        for path, data in (files or {'README.md': b'fixture\n'}).items():
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.git(work, 'add', '.')
        self.git(work, 'commit', '-m', 'initial')
        self.git(work, 'push', 'origin', branch)
        self.remotes[repo] = (remote, branch)
        return self.branch_tip(repo, branch)

    def default_branch(self, repo):
        return self.remotes[repo][1]

    def branch_tip(self, repo, branch):
        remote = self.remotes[repo][0]
        p = subprocess.run(['git', '-C', str(remote), 'rev-parse', '--verify', 'refs/heads/'+branch],
                           capture_output=True, timeout=30)
        return p.stdout.decode().strip() if p.returncode == 0 else None

    def tree(self, repo, commit, paths=None):
        remote = self.remotes[repo][0]
        entries = self.git(remote, 'ls-tree', '-rz', commit).split(b'\0')
        result = {}
        for entry in filter(None, entries):
            metadata, path = entry.split(b'\t')
            mode, kind, sha = metadata.split()
            result[path.decode()] = {'type': 'file' if mode == b'100644' else 'symlink',
                'content': self.git(remote, 'cat-file', 'blob', sha.decode())}
        return result

    def cas(self, repo, branch, expected, changes, *, base=None):
        if repo in self.denied:
            raise PermissionError('destination-denied')
        if self.before_cas:
            hook, self.before_cas = self.before_cas, None
            hook(repo, branch, expected)
        if self.branch_tip(repo, branch) != expected:
            return None
        with self.lock:
            self.counter += 1
            number = self.counter
        remote = self.remotes[repo][0]
        work = self.root / ('writer-'+str(number))
        subprocess.run(['git', 'clone', str(remote), str(work)], check=True, capture_output=True, timeout=30)
        self.git(work, 'checkout', '--detach', expected or base)
        self.git(work, 'config', 'user.email', 'fixture@example.invalid')
        self.git(work, 'config', 'user.name', 'Fixture')
        for path, data in changes.items():
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.git(work, 'add', '.')
        self.git(work, 'commit', '--allow-empty', '-m', 'distribution fixture')
        head = self.git(work, 'rev-parse', 'HEAD').decode().strip()
        # The public adapter never offers force/deletion. Real Git enforces non-fast-forward denial.
        pushed = subprocess.run(['git', '-C', str(work), 'push', 'origin', 'HEAD:refs/heads/'+branch],
                                capture_output=True, timeout=30)
        return head if pushed.returncode == 0 else None

    def file(self, repository, commit, path):
        return self.tree(repository,commit)[path]

    def release_versions(self,registration):
        tip = self.branch_tip(CONTROL_REPOSITORY,'distribution-records')
        prefix = '.distribution/releases/'+registration['configuration']['app']+'/'
        return [p[len(prefix):-5] for p in self.tree(CONTROL_REPOSITORY,tip) if p.startswith(prefix) and p.endswith('.json')]

    def status_cache(self,registration,version):
        path = f".distribution/status/{registration['configuration']['app']}/{version}.json"
        tip = self.branch_tip(CONTROL_REPOSITORY,'main')
        entry = self.tree(CONTROL_REPOSITORY,tip).get(path)
        return json.loads(entry['content']) if entry else {}


class ChannelTests(unittest.TestCase):
    def test_valid_formula_is_discoverable_and_preserves_other_app(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('brew', reg, snap)
        with tempfile.TemporaryDirectory() as directory:
            gh = LocalGit(directory)
            repo = reg['configuration']['destinations']['brew']['repository']
            gh.init(repo, files={'Formula/lumen.rb': b'other app sentinel\n'})
            with patch('distribution.validation.subprocess.run', side_effect=native_success):
                result = controller.apply_channel('brew', reg, snap, candidate, gh)
            self.assertEqual(result['state'], 'published')
            tree = gh.tree(repo, gh.branch_tip(repo, 'main'))
            self.assertEqual(tree['Formula/patronus.rb']['content'], candidate['Formula/patronus.rb'])
            self.assertEqual(tree['Formula/lumen.rb']['content'], b'other app sentinel\n')
            self.assertEqual(json.loads(tree['.distribution/channels/patronus.json']['content'])['snapshot_digest'], snap['snapshot_digest'])

    def test_unregistered_path_and_failed_validator_never_change_destination(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with tempfile.TemporaryDirectory() as directory:
            gh = LocalGit(directory)
            repo = reg['configuration']['destinations']['scoop']['repository']
            tip = gh.init(repo)
            candidate = render('scoop', reg, snap)
            candidate['bucket/lumen.json'] = b'{}'
            result = controller.apply_channel('scoop', reg, snap, candidate, gh)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(gh.branch_tip(repo, 'main'), tip)


class EvidenceGitHub(LocalGit, test_trust.GitHub):
    """GitHub API evidence and bare Git writes are external boundaries."""
    def __init__(self, root, app='patronus', version='2.7.1'):
        LocalGit.__init__(self, root)
        test_trust.GitHub.__init__(self, app, version)
        initial = self.init(CONTROL_REPOSITORY, 'distribution-records')
        self.cas(CONTROL_REPOSITORY, 'main', None, {'Formula/lumen.rb': b'other app\n'}, base=initial)
        self.remotes[CONTROL_REPOSITORY] = (self.remotes[CONTROL_REPOSITORY][0], 'main')
        self.env = 'package-authority'

    def environment(self):
        return self.env

    def controller_context(self):
        return copy.deepcopy(self.context)

    def ref(self, repo, ref):
        if repo == CONTROL_REPOSITORY and ref == 'heads/distribution-records':
            return {'type': 'commit', 'sha': self.branch_tip(repo, 'distribution-records')}
        return test_trust.GitHub.ref(self, repo, ref)

    def file(self, repo, commit, path):
        if (repo == CONTROL_REPOSITORY and commit == test_trust.CONTROL
                and path == '.distribution/registrations/'+self.registration['configuration']['app']+'.json'):
            return {'type':'file','content':encode(self.registration)}
        if path.startswith('.distribution/releases/'):
            return self.tree(repo, commit)[path]
        return test_trust.GitHub.file(self, repo, commit, path)

    def registration_revision(self, app, digest, commit):
        return {'commit': test_trust.CONTROL, 'registration': copy.deepcopy(self.registration)}


class Credentials:
    def __init__(self):
        self.minted = []
        self.expired = False

    def acquire(self, role, admission, *, repository=None):
        if self.expired:
            raise ValueError('credential-expired')
        self.minted.append(role)

    def close(self):
        return []

    def prepare_read(self,admission):
        return None


class AuthorityTests(unittest.TestCase):
    def test_missing_workflow_mode_fields_report_request_denied_without_credentials(self):
        from distribution.cli import main
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            event=Path(directory)/'event.json'
            event.write_text(json.dumps({'inputs':{'mode':'prepare','app':'patronus','run_id':'','attempt':'1'}}))
            output=Path(directory)/'result.json'
            output.write_text('{"state":"failed","failure_code":"workflow-interrupted"}')
            credentials=Credentials()
            with patch.dict('os.environ',{'GITHUB_EVENT_PATH':str(event)}),patch('sys.stdout',new=io.StringIO()),patch('sys.stderr',new=io.StringIO()):
                try:
                    code=main(['--workflow','--out',str(output)],github=gh,credentials=credentials)
                except SystemExit as error:
                    code=error.code
            self.assertEqual(code,1)
            self.assertEqual(json.loads(output.read_bytes())['failure_code'],'request-denied')
            self.assertEqual(credentials.minted,[])

    def test_workflow_data_uses_same_prepare_cli_without_shell_interpolation(self):
        from distribution.cli import main
        import io
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            original_file=gh.file
            def file(repo,commit,path):
                if path == '.distribution/registrations/patronus.json':
                    return {'type':'file','content':encode(gh.registration)}
                return original_file(repo,commit,path)
            gh.file=file
            event=Path(directory)/'event.json'
            event.write_text(json.dumps({'inputs':{'mode':'prepare','app':'patronus','run_id':'50','attempt':'1'}}))
            output=Path(directory)/'result.json'
            with patch.dict('os.environ',{'GITHUB_EVENT_PATH':str(event)}), patch('sys.stdout',new=io.StringIO()):
                code=main(['--workflow','--out',str(output)],github=gh,credentials=Credentials())
            self.assertEqual(code,0)
            self.assertEqual(json.loads(output.read_bytes())['record_ref']['version'],'2.7.1')

    @patch('sys.stdout',new_callable=io.StringIO)
    def test_owner_registration_cli_emits_approval_for_exact_config_without_repository_write(self,_stdout):
        from distribution.cli import main
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            gh.context['mode'] = 'register'
            gh.controller_run.update(actor_id=OWNER_ID,triggering_actor_id=OWNER_ID)
            root = Path(directory)
            configuration = gh.registration['configuration']
            candidate, output = root/'candidate.json', root/'approved.json'
            candidate.write_text(json.dumps(configuration))
            tip = gh.branch_tip(CONTROL_REPOSITORY,'distribution-records')
            code = main(['register','--candidate',str(candidate),'--approve-sha',canonical_digest(configuration),
                         '--out',str(output)], github=gh, credentials=Credentials())
            self.assertEqual(code,0)
            approved = json.loads(output.read_bytes())
            self.assertEqual(approved['configuration'],configuration)
            self.assertIn('/actions/runs/40/attempts/1',approved['approval']['reference'])
            self.assertEqual(gh.branch_tip(CONTROL_REPOSITORY,'distribution-records'),tip)

    def test_owner_baseline_inspection_approval_and_retry_preserve_original_record(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            gh.context['mode'] = 'inspect-baseline'
            gh.controller_run.update(actor_id=OWNER_ID,triggering_actor_id=OWNER_ID)
            credentials = Credentials()
            inspection = controller.authority(gh.registration, {'tag':'v2.7.1'}, gh, credentials)
            self.assertEqual(inspection['state'], 'inspected')
            self.assertEqual(credentials.minted, [])
            identity = inspection['candidate']
            gh.context['mode'] = 'approve-baseline'
            with self.assertRaises(ValueError):
                controller.authority(gh.registration, {'candidate':identity,'approve_sha':'f'*64}, gh, credentials)
            output = controller.authority(gh.registration, {'candidate':identity,'approve_sha':canonical_digest(identity)}, gh, credentials)
            ref = output['record_ref']
            record = gh.file(CONTROL_REPOSITORY, ref['records_commit'], '.distribution/releases/patronus/2.7.1.json')['content']
            self.assertEqual(json.loads(record)['payload']['authorization']['kind'], 'baseline')
            gh.context['mode'] = 'retry'
            retry = controller.authority(gh.registration, {'version':'2.7.1'}, gh, Credentials())
            self.assertEqual(retry['record_ref'], ref)
            self.assertEqual(gh.file(CONTROL_REPOSITORY,ref['records_commit'],'.distribution/releases/patronus/2.7.1.json')['content'], record)

    def test_admitted_run_freezes_create_only_record_and_emits_public_locator(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            credentials = Credentials()
            output = controller.authority(gh.registration, {'run_id': 50, 'attempt': 1}, gh, credentials)
            self.assertEqual(output['state'], 'recorded')
            self.assertEqual(credentials.minted, ['authority'])
            ref = output['record_ref']
            path = '.distribution/releases/patronus/2.7.1.json'
            record = json.loads(gh.file(CONTROL_REPOSITORY, ref['records_commit'], path)['content'])
            self.assertEqual(record['snapshot_digest'], ref['snapshot_digest'])
            self.assertEqual(record['payload']['identity']['assets'], gh.receipt['assets'])
            second = controller.authority(gh.registration, {'run_id': 50, 'attempt': 1}, gh, credentials)
            self.assertEqual(second['record_ref'], ref)

    def test_untrusted_or_other_app_context_mints_no_credentials_and_writes_nothing(self):
        mutations = [('ref','refs/heads/main'), ('ref','refs/heads/distribution-records'),
                     ('ref','refs/tags/distribution-control'), ('sha','f'*40), ('app','lumen')]
        for key, value in mutations:
            with self.subTest(key=key,value=value), tempfile.TemporaryDirectory() as directory:
                gh = EvidenceGitHub(directory)
                gh.context[key] = value
                credentials = Credentials()
                tip = gh.branch_tip(CONTROL_REPOSITORY, 'distribution-records')
                with self.assertRaises(ValueError):
                    controller.authority(gh.registration, {'run_id':50,'attempt':1}, gh, credentials)
                self.assertEqual(credentials.minted, [])
                self.assertEqual(gh.branch_tip(CONTROL_REPOSITORY,'distribution-records'), tip)


class PublisherTests(unittest.TestCase):
    @patch('sys.stdout',new_callable=io.StringIO)
    def test_prepare_cli_uses_protected_registration_and_same_publisher_handoff(self,_stdout):
        from distribution.cli import main
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            original_file = gh.file
            def file(repo,commit,path):
                if path == '.distribution/registrations/patronus.json':
                    return {'type':'file','content':encode(gh.registration)}
                return original_file(repo,commit,path)
            gh.file = file
            out = Path(directory)/'authority.json'
            args = ['prepare','--app','patronus','--run-id','50','--attempt','1','--out',str(out)]
            code = main(args,github=gh,credentials=Credentials())
            self.assertEqual(code,0)
            ref = json.loads(out.read_bytes())['record_ref']
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.init('darkquasar/scoop-bucket')
            with patch.dict('os.environ',{'DISTRIBUTION_RECORD_REF':json.dumps(ref)}), patch(
                    'distribution.validation.subprocess.run',side_effect=native_success):
                # Winget is deliberately unavailable; Brew and Scoop must still complete.
                code = main(args,github=gh,credentials=Credentials())
            result = json.loads(out.read_bytes())
            self.assertEqual(code,1)
            self.assertEqual(result['channels']['brew']['state'],'published')
            self.assertEqual(result['channels']['scoop']['state'],'published')

    def prepared(self, directory):
        gh = EvidenceGitHub(directory)
        ref = controller.authority(gh.registration, {'run_id':50,'attempt':1}, gh, Credentials())['record_ref']
        gh.context['role'] = 'publish'
        gh.env = 'package-publisher'
        gh.init('darkquasar/scoop-bucket', files={'bucket/lumen.json': b'other app\n'})
        return gh, ref

    def test_publisher_reads_back_record_then_publishes_and_reconciles_after_lost_status(self):
        with tempfile.TemporaryDirectory() as directory:
            gh, ref = self.prepared(directory)
            credentials = Credentials()
            output = controller.publish(gh.registration, ref, gh, credentials, channels=['scoop'])
            self.assertEqual(output['channels']['scoop']['state'], 'published')
            self.assertEqual(set(credentials.minted), {'publish'})
            tip = gh.branch_tip('darkquasar/scoop-bucket','main')
            second = controller.publish(gh.registration, ref, gh, Credentials(), channels=['scoop'])
            self.assertEqual(second['channels']['scoop']['destination_commit'], tip)
            self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'), tip)
            status = json.loads(gh.tree(CONTROL_REPOSITORY, gh.branch_tip(CONTROL_REPOSITORY,'main'))[
                '.distribution/status/patronus/2.7.1.json']['content'])
            self.assertEqual(status['channels']['scoop']['destination_commit'], tip)

    def test_forged_locator_replaced_assets_bad_rerun_and_wrong_environment_mint_no_publisher_token(self):
        import hashlib
        for change in ['digest','app','records','replacement','actor','environment','stale']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                gh, ref = self.prepared(directory)
                credentials = Credentials()
                if change == 'digest':
                    ref['snapshot_digest'] = 'f'*64
                elif change == 'app':
                    ref['app'] = 'lumen'
                elif change == 'records':
                    gh.reachable = False
                elif change == 'replacement':
                    name = next(iter(gh.assets))
                    gh.assets[name] = b'replaced'
                    gh.checksums = ''.join(hashlib.sha256(b).hexdigest()+'  '+n+'\n' for n,b in gh.assets.items()).encode()
                    gh.release_metadata['assets'] = [{'name':n,'size':len(b)} for n,b in gh.assets.items()]+[{'name':'checksums.txt','size':len(gh.checksums)}]
                elif change == 'actor':
                    gh.controller_run['triggering_actor_id'] = 8002
                elif change == 'environment':
                    gh.env = 'package-authority'
                else:
                    gh.control_tip = 'f'*40
                tip = gh.branch_tip('darkquasar/scoop-bucket','main')
                with self.assertRaises((ValueError, KeyError)):
                    controller.publish(gh.registration, ref, gh, credentials, channels=['scoop'])
                self.assertEqual(credentials.minted, [])
                self.assertEqual(gh.branch_tip('darkquasar/scoop-bucket','main'), tip)


class AdapterTests(unittest.TestCase):
    def test_publisher_grants_are_per_repository_and_one_denial_preserves_other_grants(self):
        from distribution.github import AppCredentials,Transport,APIError
        from datetime import datetime,timedelta,timezone
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            gh.context['role']='publish'
            gh.env='package-publisher'
            gh.transport=Transport()
            bodies=[]
            def service(method,path,body=None,**kwargs):
                if method == 'DELETE':
                    return None
                if path.endswith('/installation'):
                    if '/scoop-bucket/' in path:
                        raise APIError(403,'GET '+path)
                    return {'id':12}
                bodies.append(body)
                return dict(token='invented-'+str(len(bodies)),permissions={'contents':'write'},
                    expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
            gh.request=service
            creds=AppCredentials(gh,gh.registration,env={'PACKAGE_PUBLISH_APP_ID':'3',
                'PACKAGE_PUBLISH_APP_PRIVATE_KEY':'invented-key'},signer=lambda message,key:b'fixture')
            admission=controller.admit(gh.registration,gh,'publish')
            creds.acquire('publish',admission,repository=CONTROL_REPOSITORY)
            self.assertEqual(set(gh.transport.write_tokens),{CONTROL_REPOSITORY})
            with self.assertRaises(APIError):
                creds.acquire('publish',admission,repository='darkquasar/scoop-bucket')
            creds.acquire('publish',admission,repository='darkquasar/winget-pkgs')
            self.assertEqual([body['repositories'] for body in bodies],[['homebrew-tap'],['winget-pkgs']])
            self.assertEqual(set(gh.transport.write_tokens),{CONTROL_REPOSITORY,'darkquasar/winget-pkgs'})
            creds.close()

    def test_writing_roles_deny_protected_or_unregistered_destinations_before_mutation(self):
        from distribution.github import GitHub,Transport,APIError
        from urllib.error import HTTPError
        reg=fixture('patronus','registration')
        writes=[]
        def service(method,path,body=None,**kwargs):
            if method != 'GET':
                writes.append((method,path))
            if path == '/repos/'+CONTROL_REPOSITORY:
                return {'default_branch':'main'}
            self.fail('unexpected request '+path)
        gh=GitHub(request=service)
        gh.configuration=reg['configuration']
        gh.write_role='publish'
        for branch in ['distribution-control','distribution-records']:
            for expected in [None,'a'*40]:
                with self.subTest(branch=branch,expected=expected),self.assertRaises(ValueError):
                    gh.cas(CONTROL_REPOSITORY,branch,expected,{'Formula/patronus.rb':b'candidate'},base='b'*40)
        with self.assertRaises(ValueError):
            gh.cas(CONTROL_REPOSITORY,'main','a'*40,{'Formula/lumen.rb':b'other app'})
        gh.write_role='authority'
        for branch in ['distribution-control','main']:
            with self.subTest(role='authority',branch=branch),self.assertRaises(ValueError):
                gh.cas(CONTROL_REPOSITORY,branch,'a'*40,{'.distribution/releases/patronus/2.7.1.json':b'{}'})
        self.assertEqual(writes,[])
        # An invented native ruleset denies raw ref create/update/delete/force attempts.
        # This tests error handling; only commissioning can establish actual server policy.
        calls=[]
        class Ruleset:
            def open(self,request,timeout):
                calls.append(request)
                raise HTTPError(request.full_url,403,'native ruleset denial',{},None)
        transport=Transport(opener=Ruleset())
        for method,path,body in [('POST','/repos/darkquasar/homebrew-tap/git/refs',{'ref':'refs/heads/distribution-control','sha':'a'*40}),
                ('PATCH','/repos/darkquasar/homebrew-tap/git/refs/heads/distribution-records',{'sha':'a'*40,'force':False}),
                ('PATCH','/repos/darkquasar/homebrew-tap/git/refs/heads/distribution-records',{'sha':'a'*40,'force':True}),
                ('DELETE','/repos/darkquasar/homebrew-tap/git/refs/heads/distribution-control',None)]:
            before=len(calls)
            with self.assertRaises(APIError):
                transport(method,path,body,token='invented-publisher')
            self.assertEqual(len(calls),before+1)

    def test_expired_or_overbroad_installation_token_is_not_installed_and_is_revoked(self):
        from distribution.github import AppCredentials,Transport
        from datetime import datetime,timedelta,timezone
        for failure in ['expired','scope']:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                gh=EvidenceGitHub(directory)
                gh.transport=Transport()
                revoked=[]
                def service(method,path,body=None,**kwargs):
                    if method == 'DELETE':
                        revoked.append(path)
                        return None
                    if path.endswith('/installation'):
                        return {'id':8}
                    return dict(token='ephemeral-secret',
                        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1 if failure=='expired' else 60)).isoformat(),
                        permissions={'contents':'write',**({'workflows':'write'} if failure=='scope' else {})})
                gh.request=service
                creds=AppCredentials(gh,gh.registration,env={'PACKAGE_AUTHORITY_APP_ID':'1',
                    'PACKAGE_AUTHORITY_APP_PRIVATE_KEY':'invented-key'},signer=lambda message,key:b'fixture')
                with self.assertRaises(ValueError):
                    creds.acquire('authority',controller.admit(gh.registration,gh,'authority'))
                self.assertEqual(gh.transport.write_tokens,{})
                creds.close()
                self.assertEqual(revoked,['/installation/token'])

    def test_prepare_has_separate_producer_read_installation_and_transport_token_routing(self):
        from distribution.github import AppCredentials,Transport
        from datetime import datetime,timedelta,timezone
        import io
        class Opener:
            def __init__(self):
                self.requests=[]
            def open(self,request,timeout):
                self.requests.append(request)
                return io.BytesIO(b'fixture')
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            opener=Opener()
            gh.transport=Transport(read_token='tap-read-token',opener=opener)
            bodies=[]
            def service(method,path,body=None,**kwargs):
                if path.endswith('/installation'):
                    self.assertEqual(path,'/repos/darkquasar/patronus/installation')
                    return {'id':8}
                if path.endswith('/access_tokens'):
                    bodies.append(body)
                    return dict(token='producer-read-token',permissions={'actions':'read','contents':'read'},
                        expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
                if method == 'DELETE':
                    return None
                self.fail(path)
            gh.request=service
            creds=AppCredentials(gh,gh.registration,env={'SOURCE_READ_APP_ID':'2','SOURCE_READ_APP_PRIVATE_KEY':'read-key'},
                signer=lambda message,key:b'fixture')
            creds.prepare_read(controller.admit(gh.registration,gh,'authority'))
            self.assertIn('darkquasar/patronus',gh.transport.read_tokens)
            self.assertEqual(bodies,[{'repositories':['patronus'],'permissions':{'actions':'read','contents':'read'}}])
            gh.transport('GET','/repos/darkquasar/patronus/actions/artifacts/1/zip',raw=True)
            gh.transport('GET','/repos/darkquasar/homebrew-tap/git/ref/heads/distribution-control',raw=True)
            self.assertEqual([r.get_header('Authorization') for r in opener.requests],
                             ['Bearer producer-read-token','Bearer tap-read-token'])
            self.assertEqual(gh.transport.write_tokens,{})
            creds.close()
            self.assertEqual(gh.transport.read_tokens,{})

    def test_successful_204_revocation_keeps_real_baseline_cli_successful(self):
        from distribution.cli import main
        from distribution.github import AppCredentials,Transport
        from distribution.snapshot import inspect_identity
        from datetime import datetime,timedelta,timezone
        import io
        class Response(io.BytesIO):
            status=204
        class Opener:
            def open(self,request,timeout):
                return Response(b'')
        with tempfile.TemporaryDirectory() as directory:
            gh=EvidenceGitHub(directory)
            gh.context['mode']='approve-baseline'
            gh.controller_run.update(actor_id=OWNER_ID,triggering_actor_id=OWNER_ID)
            candidate=inspect_identity(gh.registration,'v2.7.1',gh)
            path=Path(directory)/'identity.json'
            path.write_bytes(encode(candidate))
            gh.transport=Transport(opener=Opener())
            def service(method,path,body=None,**kwargs):
                if method == 'DELETE':
                    return gh.transport(method,path,**kwargs)
                if path.endswith('/installation'):
                    return {'id':12}
                if path.endswith('/access_tokens'):
                    return dict(token='invented-ephemeral',permissions={'contents':'write'},
                        expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
                self.fail(path)
            gh.request=service
            creds=AppCredentials(gh,gh.registration,env={'PACKAGE_AUTHORITY_APP_ID':'1',
                'PACKAGE_AUTHORITY_APP_PRIVATE_KEY':'invented-key'},signer=lambda message,key:b'fixture')
            output=Path(directory)/'out.json'
            with patch('sys.stdout',new=io.StringIO()):
                code=main(['approve-baseline','--candidate',str(path),'--approve-sha',canonical_digest(candidate),
                    '--out',str(output)],github=gh,credentials=creds)
            self.assertEqual(code,0,json.loads(output.read_bytes()))
            self.assertEqual(json.loads(output.read_bytes())['state'],'recorded')
            self.assertEqual(gh.transport.write_tokens,{})

    def test_windows_source_consumes_authenticated_run_artifact_and_rejects_forged_actor(self):
        from distribution.github import GitHub,WindowsSource
        import hashlib,io,zipfile
        context = dict(sha='c'*40)
        reg = fixture('patronus','registration')
        evidence = dict(schema_version=1,repository=CONTROL_REPOSITORY,controller_sha='c'*40,
            run_id=80,attempt=1,job_id=90,app='patronus',package_id='darkquasar.Patronus',source='winget',
            runner_os='Windows',command=['winget','show','--id','darkquasar.Patronus','--exact','--source','winget','--versions'],
            exit=0,versions=['2.7.1'],observed_at='2026-11-07T00:00:00Z',manager_version='v1.29.380')
        actor = OWNER_ID
        def service(method,path,body=None,**kwargs):
            if '/attempts/1/jobs' in path:
                return {'jobs':[dict(id=90,name='winget-source',head_sha='c'*40,labels=['windows-2025'],
                    status='completed',conclusion='success')]}
            if path.endswith('/runs/80/attempts/1'):
                return dict(id=80,run_attempt=1,repository={'full_name':CONTROL_REPOSITORY},
                    path='.github/workflows/distribute.yml',head_sha='c'*40,head_branch='distribution-control',
                    event='workflow_dispatch',actor={'id':actor},triggering_actor={'id':actor},status='completed',conclusion='success')
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:
                archive.writestr('source.json',json.dumps(evidence))
            data=stream.getvalue()
            if path.endswith('/artifacts/1/zip'):
                return data
            if '/artifacts?' in path:
                return {'artifacts':[dict(id=1,name='winget-source-80-1',expired=False,
                    digest='sha256:'+hashlib.sha256(data).hexdigest(),workflow_run={'id':80,'head_sha':'c'*40})]}
            self.fail('unexpected source evidence API request '+path)
        gh = GitHub(context=context,request=service)
        source = WindowsSource(gh,{'run_id':80,'attempt':1})
        observed = source.observe(reg,'2.7.1')
        self.assertIn('2.7.1',observed['versions'])
        self.assertTrue(observed['verified'])
        actor = 8001
        with self.assertRaises(ValueError):
            source.observe(reg,'2.7.1')

    def test_pr_lookup_binds_exact_fork_head_and_retains_closed_feedback(self):
        from distribution.github import GitHub
        reg = fixture('patronus','registration')
        def service(method,path,body=None,**kwargs):
            if path == '/repos/microsoft/winget-pkgs':
                return {'default_branch':'master'}
            if path == '/repos/'+CONTROL_REPOSITORY:
                return {'default_branch':'main'}
            if path.endswith('/git/ref/heads/main'):
                return {'object':{'type':'commit','sha':'c'*40}}
            if path.endswith('/git/commits/'+'c'*40):
                return {'tree':{'sha':'d'*40}}
            if path.endswith('/git/trees/'+'d'*40):
                return {'tree':[]}
            if '/pulls?' in path:
                self.assertIn('head=darkquasar%3Apatronus%2F2.7.1',path)
                return [{'number':1}]
            if path.endswith('/pulls/1'):
                return dict(number=1,html_url='https://github.com/microsoft/winget-pkgs/pull/1',
                    state='closed',merged=False,head={'sha':'b'*40,'ref':'patronus/2.7.1',
                    'repo':{'full_name':'darkquasar/winget-pkgs'}},base={'repo':{'full_name':'microsoft/winget-pkgs'}})
            if '/files?' in path:
                return [{'filename':p} for p in render('winget',reg,fixture('patronus','snapshot'))]
            if '/comments?' in path:
                return [dict(body='Please correct metadata',html_url='https://github.com/comment/1')]
            if '/reviews?' in path:
                return []
            self.fail('unexpected PR API request '+path)
        gh = GitHub(request=service)
        prs = gh.pull_requests(reg,'2.7.1')
        self.assertEqual(len(prs),1)
        self.assertEqual(prs[0]['state'],'closed')
        self.assertIn('Please correct metadata',prs[0]['feedback'])

    def test_full_diff_walker_detects_unrelated_paths_without_recursive_tree_truncation(self):
        from distribution.github import GitHub
        def service(method,path,body=None,**kwargs):
            if '/git/commits/' in path:
                return {'tree':{'sha':'1'*40 if path.endswith('a'*40) else '2'*40}}
            if path.endswith('/git/trees/'+'1'*40):
                return {'tree':[dict(path='README.md',mode='100644',type='blob',sha='3'*40)]}
            if path.endswith('/git/trees/'+'2'*40):
                return {'tree':[dict(path='README.md',mode='100644',type='blob',sha='4'*40),
                    dict(path='unexpected',mode='040000',type='tree',sha='5'*40)]}
            if path.endswith('/git/trees/'+'5'*40):
                return {'tree':[dict(path='file',mode='100644',type='blob',sha='6'*40)]}
            self.fail('unexpected external request '+path)
        gh = GitHub(request=service)
        self.assertEqual(gh.diff_paths('darkquasar/winget-pkgs','a'*40,'b'*40),{'README.md','unexpected/file'})

    def test_role_token_is_repository_selected_short_lived_and_revoked_without_key_handoff(self):
        from distribution.github import AppCredentials, Transport
        import datetime
        with tempfile.TemporaryDirectory() as directory:
            gh = EvidenceGitHub(directory)
            calls = []
            expires = (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()
            def service(method,path,body=None,**kwargs):
                calls.append((method,path,body))
                if path.endswith('/installation'):
                    return {'id':12}
                if path.endswith('/access_tokens'):
                    self.assertEqual(body,{'repositories':['homebrew-tap'],'permissions':{'contents':'write'}})
                    return {'token':'ephemeral-secret','expires_at':expires,'permissions':{'contents':'write'}}
                if method == 'DELETE':
                    return None
                self.fail('unexpected credential request '+path)
            gh.request = service
            gh.transport = Transport()
            admission = controller.admit(gh.registration,gh,'authority')
            creds = AppCredentials(gh,gh.registration,env={'PACKAGE_AUTHORITY_APP_ID':'1',
                'PACKAGE_AUTHORITY_APP_PRIVATE_KEY':'invented-key'},signer=lambda message,key:b'invented-signature')
            creds.acquire('authority',admission)
            self.assertIn(CONTROL_REPOSITORY,gh.transport.write_tokens)
            self.assertEqual(gh.transport.write_tokens[CONTROL_REPOSITORY]['token'],'ephemeral-secret')
            self.assertNotIn('invented-key',repr(calls))
            self.assertEqual(gh.write_role,'authority')
            creds.close()
            self.assertEqual(gh.transport.write_tokens,{})
            self.assertTrue(any(method == 'DELETE' for method,_,_ in calls))

    def test_transport_bounds_downloads_and_strips_tokens_from_release_redirects(self):
        from distribution.github import Transport
        from urllib.error import HTTPError
        import io
        class Opener:
            def __init__(self):
                self.requests = []
                self.redirect = 'https://release-assets.githubusercontent.com/fixture'
            def open(self, request, timeout):
                self.requests.append(request)
                if request.full_url.startswith('https://github.com/'):
                    raise HTTPError(request.full_url,302,'redirect',{'Location':self.redirect},None)
                return io.BytesIO(b'0123456789')
        opener = Opener()
        transport = Transport(read_token='read-secret',opener=opener)
        data = transport('GET','https://github.com/darkquasar/patronus/releases/download/v2.7.1/test',
            raw=True,limit=10,delivery='release')
        self.assertEqual(data,b'0123456789')
        self.assertTrue(all(request.get_header('Authorization') is None for request in opener.requests))
        with self.assertRaises(ValueError):
            transport('GET','https://github.com/darkquasar/patronus/releases/download/v2.7.1/test',raw=True,limit=9,delivery='release')
        opener.redirect = 'https://attacker.invalid/stolen'
        with self.assertRaises(ValueError):
            transport('GET','https://github.com/darkquasar/patronus/releases/download/v2.7.1/test',raw=True,limit=10,delivery='release')
        self.assertFalse(any('attacker.invalid' in request.full_url for request in opener.requests))

    def test_registry_reads_exact_control_commit_and_rejects_symlink_configuration(self):
        from distribution.github import GitHub
        import base64
        reg = fixture('patronus','registration')
        mode = '100644'
        def service(method,path,body=None,**kwargs):
            if '/git/commits/' in path:
                self.assertTrue(path.endswith('c'*40))
                return {'tree':{'sha':'1'*40}}
            if path.endswith('/git/trees/'+'1'*40):
                return {'tree':[dict(path='.distribution',mode='040000',type='tree',sha='2'*40)]}
            if path.endswith('/git/trees/'+'2'*40):
                return {'tree':[dict(path='registrations',mode='040000',type='tree',sha='3'*40)]}
            if path.endswith('/git/trees/'+'3'*40):
                return {'tree':[dict(path='patronus.json',mode=mode,type='blob',sha='4'*40)]}
            if path.endswith('/git/blobs/'+'4'*40):
                return {'encoding':'base64','content':base64.b64encode(json.dumps(reg).encode()).decode()}
            self.fail('unexpected API request '+path)
        gh = GitHub(request=service)
        self.assertEqual(gh.list_registrations(CONTROL_REPOSITORY,'c'*40),[reg])
        mode = '120000'
        with self.assertRaises(ValueError):
            gh.list_registrations(CONTROL_REPOSITORY,'c'*40)

    def test_receipt_download_is_bound_to_paginated_attempt_artifact_and_api_digest(self):
        from distribution.github import GitHub
        import hashlib
        content = b'fixture ZIP bytes'
        artifact = dict(id=700, name='distribution-receipt-50-2', expired=False,
            digest='sha256:'+hashlib.sha256(content).hexdigest(),
            workflow_run={'id':50,'head_sha':'a'*40})
        responses = []
        def service(method,path,body=None,**kwargs):
            responses.append(path)
            if path.endswith('/artifacts/700/zip'):
                return content
            if 'page=2' in path:
                return {'artifacts':[copy.deepcopy(artifact)]}
            return {'artifacts':[{'id':i,'name':'unrelated'} for i in range(100)]}
        gh = GitHub(request=service)
        actual = gh.receipt_artifact('darkquasar/patronus',50,2)
        self.assertEqual(actual.get('content'), content)
        self.assertEqual(actual['run_id'], 50)
        self.assertEqual(actual['head_sha'], 'a'*40)
        artifact['digest'] = 'sha256:'+'f'*64
        with self.assertRaises(ValueError):
            gh.receipt_artifact('darkquasar/patronus',50,2)
        self.assertTrue(any('page=2' in path for path in responses))

    def test_regular_blob_reads_do_not_follow_symlink_parents_and_cas_is_non_force(self):
        from distribution.github import APIError,GitHub,RefOutcomeUnknown
        from urllib.error import URLError
        import base64
        data = b'approved formula\n'
        requests = []
        deny_ref = False
        lost_response = False
        applied_on_loss = False
        visible_tip = 'a'*40
        def service(method, path, body=None, **kwargs):
            nonlocal visible_tip
            requests.append((method,path,body))
            if path == '/repos/darkquasar/homebrew-tap':
                return {'default_branch':'main'}
            if path.endswith('/git/commits/'+'a'*40):
                return {'tree':{'sha':'b'*40}}
            if path.endswith('/git/trees/'+'b'*40):
                return {'tree':[{'path':'Formula','mode':'040000','type':'tree','sha':'c'*40},
                                {'path':'unsafe','mode':'120000','type':'blob','sha':'d'*40}]}
            if path.endswith('/git/trees/'+'c'*40):
                return {'tree':[{'path':'patronus.rb','mode':'100644','type':'blob','sha':'d'*40}]}
            if path.endswith('/git/blobs/'+'d'*40):
                return {'encoding':'base64','content':base64.b64encode(data).decode()}
            if method == 'GET' and '/git/ref/heads/' in path:
                return {'object':{'type':'commit','sha':visible_tip}}
            if method == 'POST' and path.endswith('/git/blobs'):
                return {'sha':'e'*40}
            if method == 'POST' and path.endswith('/git/trees'):
                return {'sha':'f'*40}
            if method == 'POST' and path.endswith('/git/commits'):
                return {'sha':'1'*40}
            if method == 'PATCH':
                self.assertEqual(body, {'sha':'1'*40,'force':False})
                if deny_ref:
                    raise APIError(422,'PATCH denied ref workflow permission')
                if lost_response:
                    if applied_on_loss:
                        visible_tip='1'*40
                    raise URLError('invented lost write response')
                return {'object':{'sha':'1'*40}}
            self.fail('unexpected external API request '+path)
        gh = GitHub(request=service)
        gh.write_role = 'publish'
        gh.configuration = fixture('patronus','registration')['configuration']
        tree = gh.tree('darkquasar/homebrew-tap','a'*40, paths=['Formula/patronus.rb','unsafe/other'])
        self.assertEqual(tree.get('Formula/patronus.rb'), {'type':'file','content':data})
        self.assertEqual(tree['unsafe']['type'], 'symlink')
        head = gh.cas('darkquasar/homebrew-tap','main','a'*40, {'Formula/patronus.rb':data})
        self.assertEqual(head, '1'*40)
        deny_ref = True
        with self.assertRaises(APIError):
            gh.cas('darkquasar/homebrew-tap','main','a'*40,{'Formula/patronus.rb':data})
        deny_ref = False
        lost_response = True
        with self.assertRaises(RefOutcomeUnknown) as unknown:
            gh.cas('darkquasar/homebrew-tap','main','a'*40,{'Formula/patronus.rb':data})
        self.assertEqual(unknown.exception.refs['candidate_commit'],'1'*40)
        applied_on_loss = True
        self.assertEqual(gh.cas('darkquasar/homebrew-tap','main','a'*40,{'Formula/patronus.rb':data}),'1'*40)
        for path in ['../bad', '.github/workflows/injected.yml']:
            before = len(requests)
            with self.assertRaises(ValueError):
                gh.cas('darkquasar/homebrew-tap','main','a'*40,{path:b'bad'})
            self.assertFalse(any(method in {'POST','PATCH'} for method,_,_ in requests[before:]))

    def test_actual_attempt_api_translation_uses_nested_actors_repository_and_workflow_path(self):
        from distribution.github import GitHub
        raw = dict(id=50, run_attempt=2, repository={'full_name':'darkquasar/patronus'},
            path='.github/workflows/release.yml', head_sha='a'*40, head_branch='v2.7.1',
            event='push', actor={'id':OWNER_ID}, triggering_actor={'id':OWNER_ID},
            status='completed', conclusion='success')
        requests = []
        def service(method, path, body=None, **kwargs):
            requests.append((method,path))
            return copy.deepcopy(raw)
        gh = GitHub(request=service)
        run = gh.run('darkquasar/patronus',50,2)
        self.assertEqual(run, dict(id=50, attempt=2, repository='darkquasar/patronus',
            workflow_path='.github/workflows/release.yml', head_sha='a'*40, head_branch='v2.7.1',
            event='push', actor_id=OWNER_ID, triggering_actor_id=OWNER_ID,
            status='completed', conclusion='success'))
        self.assertEqual(requests, [('GET','/repos/darkquasar/patronus/actions/runs/50/attempts/2')])
