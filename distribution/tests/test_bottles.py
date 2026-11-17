"""Bottle verification, bottled rendering and selection through public interfaces."""
import gzip
import hashlib
import io
import json
import tarfile
import unittest

from test_renderers import fixture


def executable(app, platform):
    """Synthetic release bytes whose digests the fixture snapshots record."""
    reg = fixture(app, 'registration')
    return ('synthetic:' + reg['configuration']['assets'][platform]).encode()


def members(app='patronus', version='2.7.1', binary=b'', salt=''):
    root = f'{app}/{version}'
    return [(app + '/', 'dir', None), (root + '/', 'dir', None), (root + '/bin/', 'dir', None),
            (root + '/bin/' + app, 'file', binary),
            (root + '/INSTALL_RECEIPT.json', 'file', json.dumps({'fixture': salt}).encode()),
            (root + '/.brew/', 'dir', None),
            (root + '/.brew/' + app + '.rb', 'file', b'class Fixture < Formula\nend\n'),
            (root + '/sbom.spdx.json', 'file', b'{}\n')]


def archive(entries):
    """Deterministic gzip tar from invented member tuples (name, kind, data)."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name.rstrip('/') if kind == 'dir' else name)
            info.mtime = 0
            if kind == 'dir':
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == 'file':
                info.size, info.mode = len(data), 0o555
                tar.addfile(info, io.BytesIO(data))
            elif kind == 'symlink':
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
            elif kind == 'hardlink':
                info.type, info.linkname = tarfile.LNKTYPE, data
                tar.addfile(info)
            elif kind == 'device':
                info.type, info.devmajor, info.devminor = tarfile.CHRTYPE, 1, 3
                tar.addfile(info)
            else:
                raise AssertionError(kind)
    return gzip.compress(stream.getvalue(), mtime=0)


def bottle(app='patronus', platform='darwin-arm64', salt='', version='2.7.1'):
    return archive(members(app, version, executable(app, platform), salt))


TAGS = {'darwin-arm64': 'arm64_sequoia', 'darwin-amd64': 'sequoia',
        'linux-amd64': 'x86_64_linux', 'linux-arm64': 'arm64_linux'}


def root_url(app, version):
    return f'https://github.com/darkquasar/homebrew-tap/releases/download/{app}-{version}'


def bottle_kit(reg, snap, salt=''):
    """Four verified archives keyed by remote filename plus their bottle_set."""
    from distribution.bottles import bottle_set, verify_bottle
    app = reg['configuration']['app']
    identity = snap['payload']['identity']
    archives, verified = {}, []
    for platform, tag in TAGS.items():
        binary = ('synthetic:' + reg['configuration']['assets'][platform]).encode()
        data = archive(members(app, identity['version'], binary, salt))
        result = verify_bottle(data, reg, snap, tag)
        archives[result['asset']] = data
        verified.append(result)
    return archives, verified, bottle_set(reg, snap, verified, root_url(app, identity['version']))


WINDOWS = ('windows-amd64', 'windows-arm64')
BREW = tuple(TAGS)


def native_evidence(reg, snap, bottles, *, source='fresh', run=(40, 1)):
    """A passing native-checks.json, exactly as T7's summary job writes it."""
    from distribution.bottles import bottled_formula_identity
    from distribution.state import file_digest
    from distribution.validation import render
    identity = bottled_formula_identity(reg, snap, bottles)
    candidates = {channel: file_digest(render(channel, reg, snap)) for channel in ['scoop', 'winget']}
    def entry(manager, platform, binding):
        return dict(manager=manager, platform=platform, outcome='passed', reason='',
                    runner='fixture-' + platform, image='fixture-image', image_version='20261117.1',
                    manager_version='fixture-1.0',
                    steps=dict(validate='passed', install='passed', version='passed', uninstall='passed'),
                    binding=binding)
    results = [entry('brew', platform, identity['formula_sha256']) for platform in BREW]
    results += [entry(manager, platform, candidates[manager])
                for manager in ['scoop', 'winget'] for platform in WINDOWS]
    return dict(schema_version=1, app=reg['configuration']['app'],
                version=snap['payload']['identity']['version'], snapshot_digest=snap['snapshot_digest'],
                configuration_digest=reg['configuration_digest'], candidates=candidates,
                brew=dict(source=source, bottle_set=bottles, identity=identity), results=results,
                run=dict(id=run[0], attempt=run[1],
                         url=f'https://github.com/darkquasar/homebrew-tap/actions/runs/{run[0]}/attempts/{run[1]}'))


def recorded_snapshot(gh, ref):
    from distribution.contracts import CONTROL_REPOSITORY
    path = f".distribution/releases/{ref['app']}/{ref['version']}.json"
    return json.loads(gh.file(CONTROL_REPOSITORY, ref['records_commit'], path)['content'])


def checked(gh, ref, salt='', source='fresh', selected=None):
    """Publisher inputs after passing native checks: evidence plus the checked archives."""
    snap = recorded_snapshot(gh, ref)
    if selected is None:
        archives, _verified, bottles = bottle_kit(gh.registration, snap, salt)
    else:
        archives, bottles, source = selected['archives'], selected['bottle_set'], selected['source']
    run = (gh.context['run_id'], gh.context['attempt'])
    return dict(evidence=native_evidence(gh.registration, snap, bottles, source=source, run=run),
                archives=dict(archives))


class ReleaseService:
    """Invented GitHub Releases state shared by concurrent publishers.

    Like GitHub, it lets any write-scoped caller act on any release; the operation
    log lets tests prove the publisher only touches what it created. It models one
    published release per tag, invisible drafts and asset-name uniqueness.
    """
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.releases = {}
        self.next_id = 100
        self.log = []
        self.before_publish = None
        self.failures = {}

    def ops(self, *names, run=None):
        return [entry for entry in self.log if (not names or entry[1] in names)
                and (run is None or entry[0] == run)]


class FakeReleases:
    """The github-edge release adapter double; pairs with LocalGit for formula CAS."""
    releases_service = None

    def _service(self):
        if self.releases_service is None:
            self.releases_service = ReleaseService()
        return self.releases_service

    def _tag(self, registration, version):
        return registration['configuration']['app'] + '-' + version

    def _record(self, op, release_id=None, detail=None):
        service = self._service()
        service.log.append((self.context['run_id'], op, release_id, detail))
        failure = service.failures.pop(op, None)
        if failure:
            raise failure

    def published_release(self, registration, version):
        service = self._service()
        tag = self._tag(registration, version)
        with service.lock:
            found = [r for r in service.releases.values() if r['tag'] == tag and not r['draft']]
        self._record('read', found[0]['id'] if found else None, tag)
        if not found:
            return None
        release = found[0]
        return dict(id=release['id'], tag=tag, draft=False,
                    assets=[dict(name=n, size=len(b)) for n, b in release['assets'].items()])

    def download_bottle(self, registration, version, name):
        service = self._service()
        tag = self._tag(registration, version)
        with service.lock:
            found = [r for r in service.releases.values() if r['tag'] == tag and not r['draft']]
        self._record('download', found[0]['id'] if found else None, name)
        if not found or name not in found[0]['assets']:
            raise ValueError('GitHub HTTP 404: public bottle download')
        return found[0]['assets'][name]

    def create_bottle_draft(self, registration, version, body, target):
        service = self._service()
        with service.lock:
            service.next_id += 1
            release_id = service.next_id
            service.releases[release_id] = dict(id=release_id, tag=self._tag(registration, version),
                draft=True, body=body, target=target, make_latest=False, assets={},
                creator=self.context['run_id'])
        self._record('create', release_id, self._tag(registration, version))
        return dict(id=release_id)

    def upload_bottle(self, registration, version, release_id, name, data):
        self._record('upload', release_id, name)
        service = self._service()
        with service.lock:
            release = service.releases[release_id]
            if name in release['assets']:
                raise ValueError('GitHub HTTP 422: asset already_exists')
            release['assets'][name] = bytes(data)
        return dict(name=name, size=len(data))

    def draft_assets(self, registration, version, release_id):
        self._record('draft-read', release_id)
        service = self._service()
        with service.lock:
            return dict(service.releases[release_id]['assets'])

    def publish_bottle_draft(self, registration, version, release_id):
        from distribution.github import ReleaseTagTaken
        service = self._service()
        if service.before_publish:
            service.before_publish(self.context['run_id'], release_id)
        with service.lock:
            release = service.releases[release_id]
            taken = [r for r in service.releases.values()
                     if r['tag'] == release['tag'] and not r['draft'] and r['id'] != release_id]
            if not taken:
                release['draft'] = False
        if taken:
            self._record('publish-rejected', release_id, release['tag'])
            raise ReleaseTagTaken(release['tag'])
        self._record('publish', release_id, release['tag'])

    def delete_bottle_draft(self, registration, version, release_id):
        self._record('delete', release_id)
        service = self._service()
        with service.lock:
            del service.releases[release_id]


class VerifyBottle(unittest.TestCase):
    def test_substituted_executable_is_an_identity_conflict_naming_the_digest(self):
        from distribution.bottles import BottleConflict, verify_bottle
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        substituted = b'substituted executable bytes'
        data = archive(members(binary=substituted))
        with self.assertRaises(BottleConflict) as caught:
            verify_bottle(data, reg, snap, 'arm64_sequoia')
        self.assertIn(hashlib.sha256(substituted).hexdigest(), str(caught.exception))

    def test_valid_bottle_binds_archive_executable_and_remote_filename(self):
        from distribution.bottles import verify_bottle
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        for tag, platform in [('arm64_sequoia', 'darwin-arm64'), ('sequoia', 'darwin-amd64'),
                              ('x86_64_linux', 'linux-amd64'), ('arm64_linux', 'linux-arm64')]:
            with self.subTest(tag=tag):
                data = bottle(platform=platform)
                result = verify_bottle(data, reg, snap, tag)
                asset = reg['configuration']['assets'][platform]
                self.assertEqual(result, {
                    'tag': tag, 'sha256': hashlib.sha256(data).hexdigest(),
                    'executable_sha256': snap['payload']['identity']['assets'][asset]['sha256'],
                    'asset': f'patronus-2.7.1.{tag}.bottle.tar.gz',
                    'members': sorted(['patronus', 'patronus/2.7.1', 'patronus/2.7.1/bin',
                        'patronus/2.7.1/bin/patronus', 'patronus/2.7.1/INSTALL_RECEIPT.json',
                        'patronus/2.7.1/.brew', 'patronus/2.7.1/.brew/patronus.rb',
                        'patronus/2.7.1/sbom.spdx.json'])})

    def test_tag_forms_map_to_brew_platforms_from_the_pinned_macos_list(self):
        from distribution.bottles import BottleInvalid, bottle_tag_platform
        expected = {'arm64_sequoia': ('darwin', 'arm64'), 'sequoia': ('darwin', 'amd64'),
                    'arm64_tahoe': ('darwin', 'arm64'), 'tahoe': ('darwin', 'amd64'),
                    'arm64_sonoma': ('darwin', 'arm64'), 'x86_64_linux': ('linux', 'amd64'),
                    'arm64_linux': ('linux', 'arm64')}
        for tag, platform in expected.items():
            self.assertEqual(bottle_tag_platform(tag), platform)
        for tag in ['x86_64_sequoia', 'arm64_catalina', 'catalina', 'arm64_freebsd', 'linux',
                    'ARM64_SEQUOIA', 'arm64_sequoia.1', '', 'arm64_sequoia ', 'x86_64_linux_musl',
                    'all', None, 7]:
            with self.subTest(tag=tag), self.assertRaises(BottleInvalid):
                bottle_tag_platform(tag)

    def test_structural_violations_are_invalid_and_name_the_member(self):
        from distribution.bottles import BottleInvalid, verify_bottle
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        good = members(binary=executable('patronus', 'darwin-arm64'))
        root = 'patronus/2.7.1'
        cases = {
            'symlink': (good + [(root + '/bin/link', 'symlink', 'patronus')], root + '/bin/link'),
            'executable-symlink': ([m for m in good if m[0] != root + '/bin/patronus']
                                   + [(root + '/bin/patronus', 'symlink', '/usr/bin/true')], root + '/bin/patronus'),
            'parent-traversal': (good + [(root + '/../../evil', 'file', b'x')], '../'),
            'absolute': (good + [('/etc/evil', 'file', b'x')], '/etc/evil'),
            'hard-link': (good + [(root + '/bin/copy', 'hardlink', root + '/bin/patronus')], root + '/bin/copy'),
            'device': (good + [(root + '/dev', 'device', None)], root + '/dev'),
            'extra': (good + [(root + '/share/notes.txt', 'file', b'x')], root + '/share/notes.txt'),
            'other-version': (good + [('patronus/2.7.0/bin/patronus', 'file', b'x')], 'patronus/2.7.0'),
            'duplicate': (good + [(root + '/bin/patronus', 'file', b'second')], root + '/bin/patronus'),
            'missing-executable': ([m for m in good if m[0] != root + '/bin/patronus'], root + '/bin/patronus'),
        }
        for name, (entries, text) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(BottleInvalid) as caught:
                    verify_bottle(archive(entries), reg, snap, 'arm64_sequoia')
                self.assertIn(text, str(caught.exception))
        for name, data in {'not-gzip': b'not a gzip stream',
                           'trailing': bottle() + b'trailing bytes',
                           'not-bytes': 'text'}.items():
            with self.subTest(name=name), self.assertRaises(BottleInvalid):
                verify_bottle(data, reg, snap, 'arm64_sequoia')

    def test_unknown_tag_forms_are_invalid(self):
        from distribution.bottles import BottleInvalid, verify_bottle
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        for tag in ['x86_64_sequoia', 'arm64_freebsd']:
            with self.subTest(tag=tag), self.assertRaises(BottleInvalid):
                verify_bottle(bottle(), reg, snap, tag)

    def test_uncompressed_bound_is_enforced_while_streaming(self):
        from unittest.mock import patch
        from distribution import bottles
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        self.assertEqual(bottles.MAX_UNCOMPRESSED, 256 * 1024 * 1024)
        entries = members(binary=executable('patronus', 'darwin-arm64'))
        entries[-1] = ('patronus/2.7.1/sbom.spdx.json', 'file', b'\0' * 64 * 1024)
        data = archive(entries)
        self.assertLess(len(data), 8 * 1024)
        bottles.verify_bottle(data, reg, snap, 'arm64_sequoia')
        with patch.object(bottles, 'MAX_UNCOMPRESSED', 32 * 1024):
            with self.assertRaises(bottles.BottleInvalid) as caught:
                bottles.verify_bottle(data, reg, snap, 'arm64_sequoia')
        self.assertIn('uncompressed', str(caught.exception))



class BottleSets(unittest.TestCase):
    def test_complete_set_has_one_tag_per_brew_platform_and_canonical_release(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        _archives, verified, result = bottle_kit(reg, snap)
        self.assertEqual(result, {'release_tag': 'patronus-2.7.1', 'root_url': root_url('patronus', '2.7.1'),
                                  'cellar': 'any_skip_relocation',
                                  'tags': {v['tag']: v['sha256'] for v in verified}})

    def test_incomplete_duplicate_forged_or_foreign_sets_are_rejected(self):
        from distribution.bottles import BottleConflict, BottleInvalid, bottle_set, verify_bottle
        import copy
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        _archives, verified, _ = bottle_kit(reg, snap)
        url = root_url('patronus', '2.7.1')
        with self.assertRaises(BottleInvalid):
            bottle_set(reg, snap, verified[:3], url)
        tahoe = verify_bottle(bottle(platform='darwin-arm64'), reg, snap, 'arm64_tahoe')
        with self.assertRaises(BottleInvalid):
            bottle_set(reg, snap, verified + [tahoe], url)
        with self.assertRaises(BottleInvalid):
            bottle_set(reg, snap, [tahoe] + verified[1:] + [verified[0]], url)
        for bad in [url + '/', url.replace('homebrew-tap', 'other'), root_url('lumen', '2.7.1'),
                    url.replace('https:', 'http:'), root_url('patronus', '2.7.0')]:
            with self.subTest(root_url=bad), self.assertRaises(BottleInvalid):
                bottle_set(reg, snap, verified, bad)
        forged = copy.deepcopy(verified)
        forged[0]['executable_sha256'] = 'f' * 64
        with self.assertRaises(BottleConflict):
            bottle_set(reg, snap, forged, url)
        renamed = copy.deepcopy(verified)
        renamed[0]['asset'] = 'lumen-1.4.2.arm64_sequoia.bottle.tar.gz'
        with self.assertRaises(BottleInvalid):
            bottle_set(reg, snap, renamed, url)
        lumen_reg, lumen_snap = fixture('lumen', 'registration'), fixture('lumen', 'snapshot')
        with self.assertRaises(ValueError):
            bottle_set(lumen_reg, lumen_snap, verified, url)

    def test_formula_identity_binds_bottled_bytes_set_snapshot_and_configuration(self):
        from distribution.bottles import bottled_formula_identity
        from distribution.contracts import canonical_digest
        from distribution.validation import render
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        _archives, _verified, bottles = bottle_kit(reg, snap)
        formula = render('brew', reg, snap, bottles)['Formula/patronus.rb']
        self.assertEqual(bottled_formula_identity(reg, snap, bottles), {
            'formula_sha256': hashlib.sha256(formula).hexdigest(),
            'bottle_set_sha256': canonical_digest(bottles),
            'snapshot_digest': snap['snapshot_digest'],
            'configuration_digest': reg['configuration_digest']})
        _a, _v, other = bottle_kit(reg, snap, salt='rebuilt')
        self.assertNotEqual(bottled_formula_identity(reg, snap, other)['formula_sha256'],
                            bottled_formula_identity(reg, snap, bottles)['formula_sha256'])

    def test_staging_formula_changes_only_the_root_url_line(self):
        from distribution.bottles import staging_formula
        from distribution.validation import render
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        _archives, _verified, bottles = bottle_kit(reg, snap)
        canonical = render('brew', reg, snap, bottles)['Formula/patronus.rb']
        staged = staging_formula(canonical, 'http://127.0.0.1:8765/bottles')
        old, new = canonical.decode().splitlines(), staged.decode().splitlines()
        self.assertEqual(len(old), len(new))
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(changed, [('    root_url "' + bottles['root_url'] + '"',
                                    '    root_url "http://127.0.0.1:8765/bottles"')])
        for bad in ['https://example.com/bottles', 'http://127.0.0.1:8765/x"\n    system "id',
                    'http://127.0.0.1:8765/#{raise}', 'http://127.0.0.1:8765/a\nb', 'file:///tmp/x',
                    'http://127.0.0.1.evil.test/x', 'http://user@127.0.0.1/x']:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                staging_formula(canonical, bad)
        unbottled = render('brew', reg, snap)['Formula/patronus.rb']
        doubled = canonical.replace(b'  bottle do\n', b'  bottle do\n    root_url "https://x"\n')
        for bad_canonical in [unbottled, doubled, canonical + b'  root_url "https://y"\n']:
            with self.subTest(canonical=bad_canonical[-40:]), self.assertRaises(ValueError):
                staging_formula(bad_canonical, 'http://127.0.0.1:8765/bottles')



class VerifyBottleCli(unittest.TestCase):
    """Offline T7 seam: no credentials, no network, exit code per failure class."""
    def run_cli(self, directory, data, tag='arm64_sequoia', link=False):
        import os
        from pathlib import Path
        from unittest.mock import patch
        from distribution import cli
        root = Path(directory)
        for kind in ['registration', 'snapshot']:
            (root / f'{kind}.json').write_text(json.dumps(fixture('patronus', kind)))
        target = root / 'bottle.tar.gz'
        if link:
            (root / 'real.tar.gz').write_bytes(data)
            os.symlink(root / 'real.tar.gz', target)
        else:
            target.write_bytes(data)
        out = root / 'result.json'
        def denied(*args, **kwargs):
            raise AssertionError('verify-bottle must not build a network adapter')
        with patch.object(cli, 'adapter', denied), patch.object(cli, 'GitHub', denied), \
                patch.dict('os.environ', {}, clear=True), patch('sys.stdout', new=io.StringIO()):
            code = cli.main(['verify-bottle', '--archive', str(target), '--tag', tag,
                             '--registration', str(root / 'registration.json'),
                             '--snapshot', str(root / 'snapshot.json'), '--out', str(out)])
        return code, json.loads(out.read_bytes())

    def test_exit_codes_and_result_json(self):
        import tempfile
        from distribution.bottles import verify_bottle
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with tempfile.TemporaryDirectory() as directory:
            code, document = self.run_cli(directory, bottle())
            self.assertEqual(code, 0)
            self.assertEqual(document, verify_bottle(bottle(), reg, snap, 'arm64_sequoia'))
        substituted = b'substituted executable bytes'
        with tempfile.TemporaryDirectory() as directory:
            code, document = self.run_cli(directory, archive(members(binary=substituted)))
            self.assertEqual(code, 3)
            self.assertEqual(document['error'], 'BottleConflict')
            self.assertIn(hashlib.sha256(substituted).hexdigest(), document['message'])
        extra = members(binary=executable('patronus', 'darwin-arm64')) + [
            ('patronus/2.7.1/share/extra', 'file', b'x')]
        for data, tag, link, text in [(archive(extra), 'arm64_sequoia', False, 'patronus/2.7.1/share/extra'),
                                      (bottle(), 'x86_64_sequoia', False, 'x86_64_sequoia'),
                                      (bottle(), 'arm64_sequoia', True, 'symlink')]:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                code, document = self.run_cli(directory, data, tag, link)
                self.assertEqual(code, 2)
                self.assertEqual(document['error'], 'BottleInvalid')
                self.assertIn(text, document['message'])



class NativeChecksSchema(unittest.TestCase):
    def test_example_document_for_the_summary_job_passes_every_channel_gate(self):
        from pathlib import Path
        from distribution.bottles import native_failures, validate_native_checks
        from distribution.state import file_digest
        from distribution.validation import render
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        path = Path(__file__).parent / 'fixtures' / 'bottles' / 'native-checks.example.json'
        document = json.loads(path.read_text())
        self.assertEqual(validate_native_checks(document), document)
        bottles = document['brew']['bottle_set']
        formula = render('brew', reg, snap, bottles)['Formula/patronus.rb']
        context = dict(run_id=40, attempt=1)
        self.assertEqual(native_failures('brew', reg, snap, document, hashlib.sha256(formula).hexdigest(),
                                         context, bottles), [])
        for channel in ['scoop', 'winget']:
            self.assertEqual(native_failures(channel, reg, snap, document, file_digest(render(channel, reg, snap)),
                                             context), [])
        self.assertIn('statically-mapped', [r['outcome'] for r in document['results']])


class ReleaseAdapter(unittest.TestCase):
    """The production GitHub release methods against an invented REST service."""
    def adapter(self, responses):
        from distribution.github import GitHub
        calls = []
        def request(method, path, body=None, **options):
            calls.append((method, path, body, options))
            for (m, prefix), value in responses.items():
                if m == method and path.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
                    return value(method, path, body) if callable(value) else value
            raise AssertionError(f'unexpected request {method} {path}')
        gh = GitHub(request=request)
        gh.write_role = 'publish'
        gh.configuration = fixture('patronus', 'registration')['configuration']
        return gh, calls

    def test_draft_lifecycle_targets_only_the_tap_tag_and_drafts_it_created(self):
        from distribution.github import APIError, ReleaseTagTaken
        reg = fixture('patronus', 'registration')
        api = '/repos/darkquasar/homebrew-tap/releases'
        name = 'patronus-2.7.1.arm64_sequoia.bottle.tar.gz'
        state = {'published': None}
        def by_tag(method, path, body):
            if state['published'] is None:
                raise APIError(404, 'GET ' + path)
            return dict(id=state['published'], tag_name='patronus-2.7.1', draft=False)
        responses = {
            ('GET', api + '/tags/'): by_tag,
            ('POST', api): dict(id=7, draft=True, tag_name='patronus-2.7.1'),
            ('POST', 'https://uploads.github.com' + api + '/7/assets'): dict(id=70, name=name, size=4, state='uploaded'),
            ('GET', api + '/7/assets'): [dict(id=70, name=name, size=4, state='uploaded')],
            ('GET', api + '/assets/70'): b'data',
            ('GET', api + '/7'): dict(id=7, draft=True, tag_name='patronus-2.7.1'),
            ('PATCH', api + '/7'): APIError(422, 'PATCH ' + api + '/7'),
            ('DELETE', api + '/7'): None,
            ('GET', api + '/9/assets'): [],
        }
        gh, calls = self.adapter(responses)
        self.assertIsNone(gh.published_release(reg, '2.7.1'))
        self.assertEqual(gh.create_bottle_draft(reg, '2.7.1', 'run 40', 'a' * 40), {'id': 7})
        self.assertEqual(calls[-1][2], dict(tag_name='patronus-2.7.1', target_commitish='a' * 40,
            name='patronus-2.7.1', body='run 40', draft=True, prerelease=False, make_latest='false'))
        gh.upload_bottle(reg, '2.7.1', 7, name, b'data')
        self.assertEqual(calls[-1][1], 'https://uploads.github.com' + api + '/7/assets?name=' + name)
        self.assertEqual(calls[-1][3]['content'], b'data')
        self.assertEqual(calls[-1][3]['delivery'], 'upload')
        self.assertEqual(gh.draft_assets(reg, '2.7.1', 7), {name: b'data'})
        self.assertTrue(all(c[3].get('privileged') for c in calls[-3:]))
        state['published'] = 9
        with self.assertRaises(ReleaseTagTaken):
            gh.publish_bottle_draft(reg, '2.7.1', 7)
        gh.delete_bottle_draft(reg, '2.7.1', 7)
        self.assertEqual(calls[-1][:2], ('DELETE', api + '/7'))
        count = len(calls)
        lumen = fixture('lumen', 'registration')
        for action in [lambda: gh.upload_bottle(reg, '2.7.1', 9, name, b'data'),
                       lambda: gh.publish_bottle_draft(reg, '2.7.1', 9),
                       lambda: gh.delete_bottle_draft(reg, '2.7.1', 9),
                       lambda: gh.delete_bottle_draft(reg, '2.7.1', 7),
                       lambda: gh.create_bottle_draft(lumen, '1.4.2', 'x', 'a' * 40),
                       lambda: gh.upload_bottle(reg, '2.7.1', 7, '../evil.bottle.tar.gz', b'x')]:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(len(calls), count)
        gh.write_role = None
        with self.assertRaises(ValueError):
            gh.create_bottle_draft(reg, '2.7.1', 'x', 'a' * 40)
        self.assertEqual(len(calls), count)

    def test_published_draft_is_never_deleted_and_publish_reads_back(self):
        reg = fixture('patronus', 'registration')
        api = '/repos/darkquasar/homebrew-tap/releases'
        responses = {('POST', api): dict(id=7, draft=True, tag_name='patronus-2.7.1'),
                     ('GET', api + '/7'): dict(id=7, draft=False, tag_name='patronus-2.7.1'),
                     ('PATCH', api + '/7'): dict(id=7, draft=False, tag_name='patronus-2.7.1')}
        gh, calls = self.adapter(responses)
        gh.create_bottle_draft(reg, '2.7.1', 'x', 'a' * 40)
        gh.publish_bottle_draft(reg, '2.7.1', 7)
        self.assertEqual(calls[-1][2], dict(draft=False, tag_name='patronus-2.7.1', make_latest='false'))
        with self.assertRaises(ValueError):
            gh.delete_bottle_draft(reg, '2.7.1', 7)
        self.assertNotIn('DELETE', [c[0] for c in calls])

    def test_transport_allows_only_tap_release_hosts_and_routes_tokens(self):
        import time
        from urllib.error import HTTPError
        from distribution.github import Transport
        class Opener:
            def __init__(self):
                self.requests = []
                self.redirect = 'https://release-assets.githubusercontent.com/fixture'
            def open(self, request, timeout):
                self.requests.append(request)
                host = request.full_url.split('/')[2]
                if host in {'github.com'} or request.full_url.endswith('/releases/assets/70'):
                    raise HTTPError(request.full_url, 302, 'redirect', {'Location': self.redirect}, None)
                if host == 'uploads.github.com':
                    return io.BytesIO(json.dumps(dict(id=70, name='x', size=4, state='uploaded')).encode())
                return io.BytesIO(b'0123')
        opener = Opener()
        transport = Transport(read_token='read-secret', opener=opener)
        transport.write_tokens['darkquasar/homebrew-tap'] = dict(token='write-secret', expires_at=time.time() + 3600)
        tap = 'https://github.com/darkquasar/homebrew-tap/releases/download/patronus-2.7.1/x.bottle.tar.gz'
        self.assertEqual(transport('GET', tap, raw=True, limit=4, delivery='release'), b'0123')
        self.assertTrue(all(r.get_header('Authorization') is None for r in opener.requests))
        opener.requests.clear()
        transport('POST', 'https://uploads.github.com/repos/darkquasar/homebrew-tap/releases/7/assets?name=x.bottle.tar.gz',
                  content=b'data', delivery='upload')
        upload = opener.requests[-1]
        self.assertEqual((upload.get_header('Authorization'), upload.get_header('Content-type'), upload.data),
                         ('Bearer write-secret', 'application/octet-stream', b'data'))
        opener.requests.clear()
        self.assertEqual(transport('GET', '/repos/darkquasar/homebrew-tap/releases/assets/70', raw=True, limit=4,
                                   delivery='asset', privileged=True), b'0123')
        self.assertEqual([r.get_header('Authorization') for r in opener.requests], ['Bearer write-secret', None])
        denied = [('GET', 'https://github.com/darkquasar/patronus/releases/download/patronus-2.7.1/x', dict(delivery='release')),
                  ('GET', 'https://github.com/darkquasar/homebrew-tap/releases/download/patronus-latest/x', dict(delivery='release')),
                  ('POST', 'https://uploads.github.com/repos/darkquasar/scoop-bucket/releases/7/assets?name=x', dict(delivery='upload', content=b'x')),
                  ('POST', 'https://uploads.github.com/repos/darkquasar/homebrew-tap/releases/7/assets?name=x', dict(content=b'x')),
                  ('GET', 'https://uploads.github.com/repos/darkquasar/homebrew-tap/releases/7/assets?name=x', dict(delivery='upload')),
                  ('GET', '/repos/darkquasar/scoop-bucket/releases/assets/70', dict(delivery='asset', raw=True)),
                  ('POST', '/repos/darkquasar/homebrew-tap/releases', dict(content=b'x'))]
        for method, url, options in denied:
            count = len(opener.requests)
            with self.subTest(url=url, options=options), self.assertRaises(ValueError):
                transport(method, url, **options)
            self.assertEqual(len(opener.requests), count)
        opener.redirect = 'https://attacker.invalid/stolen'
        with self.assertRaises(ValueError):
            transport('GET', '/repos/darkquasar/homebrew-tap/releases/assets/70', raw=True, delivery='asset', privileged=True)
        self.assertFalse(any('attacker.invalid' in r.full_url for r in opener.requests))


if __name__ == '__main__':
    unittest.main()


class ReviewCorrections(unittest.TestCase):
    """Codex T6 milestone review T6R-01..03 and the plan's digest-conflict contract."""
    def sparse_archive(self, sparse_name):
        entries = members(binary=executable('patronus', 'darwin-arm64'))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as tar:
            for name, kind, data in entries:
                info = tarfile.TarInfo(name.rstrip('/') if kind == 'dir' else name)
                info.mtime = 0
                if kind == 'dir':
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                    continue
                info.size, info.mode = len(data), 0o555
                if name == sparse_name:
                    info.pax_headers = {'GNU.sparse.map': f'65535,{len(data)}', 'GNU.sparse.size': '65536'}
                tar.addfile(info, io.BytesIO(data))
        return gzip.compress(stream.getvalue(), mtime=0)

    def test_sparse_members_are_rejected_before_their_logical_size_is_read(self):
        from unittest.mock import patch
        from distribution import bottles
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        for name in ['patronus/2.7.1/sbom.spdx.json', 'patronus/2.7.1/bin/patronus']:
            data = self.sparse_archive(name)
            self.assertLess(len(data), 8 * 1024)
            with self.subTest(member=name), patch.object(bottles, 'MAX_UNCOMPRESSED', 32 * 1024):
                with self.assertRaises(bottles.BottleInvalid) as caught:
                    bottles.verify_bottle(data, reg, snap, 'arm64_sequoia')
                self.assertIn('sparse', str(caught.exception))
                self.assertIn(name, str(caught.exception))

    def test_executable_of_another_platform_is_an_identity_conflict(self):
        import tempfile
        from distribution.bottles import BottleConflict, verify_bottle
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with self.assertRaises(BottleConflict) as caught:
            verify_bottle(bottle(platform='darwin-amd64'), reg, snap, 'arm64_sequoia')
        self.assertIn('darwin-amd64', str(caught.exception))
        with tempfile.TemporaryDirectory() as directory:
            code, document = VerifyBottleCli.run_cli(self, directory, bottle(platform='darwin-amd64'))
        self.assertEqual((code, document['error']), (3, 'BottleConflict'))

    def adapter_after_publish(self):
        reg = fixture('patronus', 'registration')
        api = '/repos/darkquasar/homebrew-tap/releases'
        responses = {('POST', api): dict(id=7, draft=True, tag_name='patronus-2.7.1'),
                     ('PATCH', api + '/7'): dict(id=7, draft=False, tag_name='patronus-2.7.1')}
        return reg, ReleaseAdapter.adapter(self, responses)

    def test_own_draft_authority_ends_at_publication_and_is_bound_to_its_tag(self):
        name = 'patronus-2.7.1.arm64_sequoia.bottle.tar.gz'
        reg, (gh, calls) = self.adapter_after_publish()
        gh.create_bottle_draft(reg, '2.7.1', 'x', 'a' * 40)
        count = len(calls)
        for action in [lambda: gh.upload_bottle(reg, '2.7.2', 7, name.replace('2.7.1', '2.7.2'), b'data'),
                       lambda: gh.publish_bottle_draft(reg, '2.7.2', 7),
                       lambda: gh.draft_assets(reg, '2.7.2', 7),
                       lambda: gh.delete_bottle_draft(reg, '2.7.2', 7)]:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(len(calls), count)
        gh.publish_bottle_draft(reg, '2.7.1', 7)
        count = len(calls)
        for action in [lambda: gh.upload_bottle(reg, '2.7.1', 7, name, b'data'),
                       lambda: gh.publish_bottle_draft(reg, '2.7.1', 7),
                       lambda: gh.publish_bottle_draft(reg, '2.7.2', 7),
                       lambda: gh.draft_assets(reg, '2.7.1', 7)]:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(len(calls), count)

    def example(self):
        from pathlib import Path
        path = Path(__file__).parent / 'fixtures' / 'bottles' / 'native-checks.example.json'
        return json.loads(path.read_text())

    def test_malformed_native_evidence_is_invalid_and_fails_every_gate_closed(self):
        import copy
        from distribution.bottles import NativeEvidenceInvalid, native_failures, validate_native_checks
        from distribution.state import file_digest
        from distribution.validation import render
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        def mutate(change):
            document = copy.deepcopy(self.example())
            change(document)
            return document
        cases = {
            'bottle_set unknown field': lambda d: d['brew']['bottle_set'].update(unexpected=True),
            'bottle_set missing field': lambda d: d['brew']['bottle_set'].pop('cellar'),
            'bottle_set tags not digests': lambda d: d['brew']['bottle_set'].update(tags={'arm64_sequoia': 1}),
            'brew source list': lambda d: d['brew'].update(source=[]),
            'schema_version text': lambda d: d.update(schema_version='1'),
            'app list': lambda d: d.update(app=['patronus']),
            'candidates digest int': lambda d: d['candidates'].update(scoop=1),
            'manager list': lambda d: d['results'][0].update(manager=['brew']),
            'platform dict': lambda d: d['results'][0].update(platform={}),
            'outcome list': lambda d: d['results'][0].update(outcome=['passed']),
            'step value list': lambda d: d['results'][0]['steps'].update(install=['passed']),
            'binding int': lambda d: d['results'][0].update(binding=7),
            'results dict': lambda d: d.update(results={}),
            'run id text': lambda d: d['run'].update(id='40'),
        }
        context = dict(run_id=40, attempt=1)
        for label, change in cases.items():
            document = mutate(change)
            with self.subTest(case=label):
                with self.assertRaises(NativeEvidenceInvalid):
                    validate_native_checks(document)
                for channel in ['scoop', 'winget']:
                    failing = native_failures(channel, reg, snap, document,
                                              file_digest(render(channel, reg, snap)), context)
                    self.assertTrue(failing and failing[0].startswith('native-checks.json invalid'), failing)
