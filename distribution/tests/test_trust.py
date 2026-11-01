import copy
import hashlib
import io
import json
import unittest
import zipfile

from distribution import contracts, trust
from test_contracts import registration, approve

CONTROL = 'c' * 40
COMMIT = 'a' * 40
RECORDS = 'd' * 40


class GitHub:
    """Invented external service, not an owned-module mock. No real credentials."""
    def __init__(self, app='patronus', version='2.7.1'):
        self.registration = registration(app)
        self.registrations = [self.registration]
        c = self.registration['configuration']
        self.context = dict(repository=contracts.CONTROL_REPOSITORY,
            ref='refs/heads/distribution-control', sha=CONTROL,
            run_id=40, attempt=1, mode='prepare', role='authority', app=app)
        self.controller_run = dict(id=40, attempt=1, repository=contracts.CONTROL_REPOSITORY,
            workflow_path='.github/workflows/distribute.yml', head_sha=CONTROL,
            event='workflow_dispatch', head_branch='distribution-control',
            actor_id=c['dispatcher_actor_id'], triggering_actor_id=c['dispatcher_actor_id'])
        self.release_run = dict(id=50, attempt=1, repository=c['repository'],
            workflow_path=c['release_workflow_path'], head_sha=COMMIT, event='push',
            head_branch='v'+version, actor_id=contracts.OWNER_ID,
            triggering_actor_id=contracts.OWNER_ID, status='completed', conclusion='success')
        self.control_tip = CONTROL
        self.records_tip = RECORDS
        self.source_commit = COMMIT
        self.ancestry = True
        self.reachable = True
        self.owner_id = contracts.OWNER_ID
        self.workflow = b'name: release\nsteps: [{go-version: "1.25"}]\n'
        self.assets = {name: ('synthetic:' + name).encode() for name in c['assets'].values()}
        self.checksums = ''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n'
                                for name,data in self.assets.items()).encode()
        self.release_metadata = dict(tag_name='v'+version, draft=False, prerelease=False,
            assets=[{'name':n, 'size':len(b)} for n,b in self.assets.items()]
                   + [{'name':'checksums.txt','size':len(self.checksums)}])
        self.receipt = dict(schema_version=1, repository=c['repository'],tag='v'+version,
            version=version, commit=COMMIT, run_id=50, attempt=1,
            workflow_path=c['release_workflow_path'],workflow_revision=COMMIT,
            workflow_sha256=hashlib.sha256(self.workflow).hexdigest(),event='push',
            ref='refs/tags/v'+version,actor_id=contracts.OWNER_ID,
            triggering_actor_id=contracts.OWNER_ID,
            checksums_sha256=hashlib.sha256(self.checksums).hexdigest(),
            assets={n:{'size':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in self.assets.items()})
        self.artifact_receipt = copy.deepcopy(self.receipt)
        self.file_kind = 'file'
        self.record_documents = {}
        self.artifact_missing = False
        self.nested_tag = False

    def repository(self, repository):
        return {'full_name':repository, 'owner_id':self.owner_id}

    def ref(self, repository, ref):
        if repository == contracts.CONTROL_REPOSITORY:
            return {'type':'commit','sha':self.control_tip if ref == 'heads/distribution-control' else self.records_tip}
        if ref.startswith('tags/') and self.nested_tag:
            return {'type':'tag','sha':'b'*40}
        return {'type':'commit','sha':self.source_commit}

    def tag(self, repository, sha):
        if sha == 'b'*40:
            return {'type':'tag','sha':'e'*40}
        return {'type':'commit','sha':self.source_commit}

    def is_ancestor(self, repository, ancestor, tip):
        return self.reachable if repository == contracts.CONTROL_REPOSITORY else self.ancestry

    def run(self, repository, run_id, attempt):
        return copy.deepcopy(self.controller_run if repository == contracts.CONTROL_REPOSITORY else self.release_run)

    def user(self, actor_id):
        return {'id':actor_id, 'type':'Bot','login':'fixture-dispatch[bot]'}

    def app(self, slug):
        return {'id':self.registration['configuration']['dispatcher_app_id'],'slug':slug}

    def list_registrations(self, repository, commit):
        if commit != CONTROL:
            raise ValueError('untrusted registration revision')
        return copy.deepcopy(self.registrations)

    def file(self, repository, commit, path):
        if path.startswith('.distribution/releases/'):
            data = self.record_documents[commit]
            return {'type':self.file_kind,'content':json.dumps(data).encode()}
        return {'type':self.file_kind,'content':self.workflow}

    def release(self, repository, tag):
        return copy.deepcopy(self.release_metadata)

    def download_asset(self, repository, tag, name):
        return self.checksums if name == 'checksums.txt' else self.assets[name]

    def receipt_artifact(self, repository, run_id, attempt):
        if self.artifact_missing:
            raise ValueError('receipt-unavailable')
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(zipfile.ZipInfo('receipt.json'), json.dumps(self.artifact_receipt))
        data = stream.getvalue()
        return {'id':700,'name':f'distribution-receipt-{run_id}-{attempt}',
                'run_id':run_id,'head_sha':COMMIT,'expired':False,
                'sha256':hashlib.sha256(data).hexdigest(),'content':data}


class ControllerAdmissionTests(unittest.TestCase):
    def test_wrong_rerun_actor_is_denied(self):
        github = GitHub()
        github.controller_run['triggering_actor_id'] = 8002
        with self.assertRaises(ValueError):
            trust.authorize_controller(github.registration, github.context, github)

    def test_only_current_protected_workflow_and_api_actors_are_admitted(self):
        for field, value in [('ref','refs/heads/feature'),('ref','refs/tags/distribution-control'),
                ('ref','refs/heads/distribution-records'),('sha','f'*40),
                ('repository','attacker/homebrew-tap'),('mode','arbitrary'),('role','recorder'),
                ('app','lumen')]:
            github = GitHub()
            github.context[field] = value
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                trust.authorize_controller(github.registration,github.context,github)
        for field,value in [('actor_id',8002),('event','pull_request'),('head_sha','f'*40),
                            ('workflow_path','.github/workflows/untrusted.yml')]:
            github = GitHub()
            github.controller_run[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                trust.authorize_controller(github.registration,github.context,github)

    def test_missing_tip_forged_registration_and_shared_dispatchers_are_denied(self):
        for change in ['tip','registration','dispatcher']:
            github = GitHub()
            if change == 'tip':
                github.control_tip = None
            elif change == 'registration':
                github.registration = approve({**github.registration, 'configuration':
                    {**github.registration['configuration'],'description':'forged'}})
            else:
                second = registration('lumen')
                second['configuration']['dispatcher_actor_id'] = 8001
                github.registrations.append(approve(second))
            with self.subTest(change=change), self.assertRaises(ValueError):
                trust.authorize_controller(github.registration,github.context,github)

    def test_valid_prepare_and_owner_modes_with_fresh_dispatch_recovery(self):
        github = GitHub()
        for role in ['authority','publish']:
            github.context['role'] = role
            admitted = trust.authorize_controller(github.registration,github.context,github)
            self.assertEqual(admitted['controller_sha'],CONTROL)
        github.controller_run.update(actor_id=contracts.OWNER_ID,triggering_actor_id=contracts.OWNER_ID)
        for mode in ['register','inspect-baseline','approve-baseline','retry','status']:
            github.context.update(mode=mode,role='authority')
            self.assertEqual(trust.authorize_controller(github.registration,github.context,github)['mode'],mode)
            github.controller_run['triggering_actor_id'] = 8001
            with self.assertRaises(ValueError):
                trust.authorize_controller(github.registration,github.context,github)
            github.controller_run['triggering_actor_id'] = contracts.OWNER_ID
        github.context.update(mode='retry',sha='f'*40)
        with self.assertRaises(ValueError):
            trust.authorize_controller(github.registration,github.context,github)
        github.context['sha'] = CONTROL
        self.assertEqual(trust.authorize_controller(github.registration,github.context,github)['controller_sha'],CONTROL)


class ReleaseVerificationTests(unittest.TestCase):
    def test_nonowner_release_cannot_create_authorized_snapshot(self):
        github = GitHub()
        github.release_run['actor_id'] = 123
        with self.assertRaises(ValueError):
            trust.verify_release(github.registration,github.receipt,github)

    def test_owner_run_requires_exact_repository_workflow_attempt_and_history(self):
        for field,value in [('repository','fork/patronus'),('triggering_actor_id',123),
            ('workflow_path','.github/workflows/other.yml'),('event','pull_request'),
            ('attempt',2),('id',51),('conclusion','failure'),('status','in_progress'),
            ('head_branch','main'),('head_sha','f'*40)]:
            github = GitHub()
            github.release_run[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                trust.verify_release(github.registration,github.receipt,github)
        github = GitHub()
        github.ancestry = False
        with self.assertRaises(ValueError):
            trust.verify_release(github.registration,github.receipt,github)

    def test_receipt_is_read_from_run_artifact_and_workflow_hash_is_audit_only(self):
        github = GitHub()
        github.nested_tag = True
        snapshot = trust.verify_release(github.registration,github.receipt,github)
        self.assertEqual(snapshot['payload']['identity']['commit'],COMMIT)
        github.workflow = b'changed Go setup version, still owner-authorized'
        github.receipt['workflow_sha256'] = hashlib.sha256(github.workflow).hexdigest()
        github.artifact_receipt = copy.deepcopy(github.receipt)
        newer = trust.verify_release(github.registration,github.receipt,github)
        self.assertEqual(newer['payload']['identity'],snapshot['payload']['identity'])
        github.artifact_missing = True
        with self.assertRaises(ValueError):
            trust.verify_release(github.registration,github.receipt,github)
        github.artifact_missing = False
        github.receipt['workflow_sha256'] = '0'*64
        with self.assertRaises(ValueError):
            trust.verify_release(github.registration,github.receipt,github)

    def test_only_strict_stable_published_release_bytes_are_accepted(self):
        for version in ['02.7.1','2.7','2.7.1-beta','2.7.1+build']:
            github = GitHub(version=version)
            with self.subTest(version=version), self.assertRaises(ValueError):
                trust.verify_release(github.registration,github.receipt,github)
        for change in ['draft','prerelease','missing','duplicate','checksum','replacement','commit','artifact']:
            github = GitHub()
            name = next(iter(github.assets))
            if change in ['draft','prerelease']:
                github.release_metadata[change] = True
            elif change == 'missing':
                github.release_metadata['assets'].pop(0)
            elif change == 'duplicate':
                github.checksums += github.checksums.splitlines(keepends=True)[0]
            elif change == 'checksum':
                github.checksums = github.checksums.replace(name.encode(), b'wrong-name')
            elif change == 'replacement':
                github.assets[name] = b'replaced'
                github.checksums = ''.join(hashlib.sha256(data).hexdigest()+'  '+n+'\n'
                    for n,data in github.assets.items()).encode()
            elif change == 'commit':
                github.source_commit = 'f'*40
            else:
                github.artifact_receipt['actor_id'] = 123
            with self.subTest(change=change), self.assertRaises(ValueError):
                trust.verify_release(github.registration,github.receipt,github)

    def test_second_app_has_its_own_release_and_all_six_assets(self):
        github = GitHub('lumen','1.4.2')
        result = trust.verify_release(github.registration,github.receipt,github)
        identity = result['payload']['identity']
        self.assertEqual(identity['repository'],'darkquasar/lumen')
        self.assertEqual(identity['version'],'1.4.2')
        self.assertEqual(set(identity['assets']),set(github.assets))
        with self.assertRaises(ValueError):
            trust.verify_release(registration('patronus'),github.receipt,github)

    def test_checksum_structure_unsafe_workflow_and_cyclic_tags_are_denied(self):
        for change in ['duplicate','missing','malformed','symlink','cycle']:
            github = GitHub()
            if change == 'duplicate':
                github.checksums += github.checksums.splitlines(keepends=True)[0]
            elif change == 'missing':
                github.checksums = b'\n'.join(github.checksums.splitlines()[1:])+b'\n'
            elif change == 'malformed':
                github.checksums = b'not a checksum\n'
            elif change == 'symlink':
                github.file_kind = 'symlink'
            else:
                github.nested_tag = True
                github.tag = lambda repo,sha: {'type':'tag','sha':sha}
            github.release_metadata['assets'][-1]['size'] = len(github.checksums)
            with self.subTest(change=change), self.assertRaises(ValueError):
                trust.verify_release(github.registration,github.receipt,github)


class ReviewCorrectionTests(unittest.TestCase):
    def test_prepare_checks_bot_and_app_through_public_lookup_coordinates(self):
        for changes in [dict(id=123),dict(type='User'),dict(login='not-a-bot')]:
            gh = GitHub()
            gh.user = lambda actor, changes=changes: {'id':actor,'type':'Bot',
                'login':'fixture-dispatch[bot]',**changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                trust.authorize_controller(gh.registration,gh.context,gh)
        gh = GitHub()
        gh.app = lambda slug: {'id':999,'slug':slug}
        with self.assertRaises(ValueError):
            trust.authorize_controller(gh.registration,gh.context,gh)

    def test_receipt_artifact_requires_matching_source_commit(self):
        gh=GitHub()
        original=gh.receipt_artifact
        def artifact(*args):
            return {**original(*args),'head_sha':'f'*40}
        gh.receipt_artifact=artifact
        with self.assertRaises(ValueError):
            trust.verify_release(gh.registration,gh.receipt,gh)

    def test_owner_modes_require_owner_as_original_actor(self):
        gh=GitHub()
        gh.context['mode']='retry'
        gh.controller_run.update(actor_id=8001,triggering_actor_id=contracts.OWNER_ID)
        with self.assertRaises(ValueError):
            trust.authorize_controller(gh.registration,gh.context,gh)
