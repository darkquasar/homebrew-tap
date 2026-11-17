"""Native-check inputs through distribution.native.prepare's public functions."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from test_renderers import fixture
from distribution.state import file_digest
from distribution.validation import render

LOOPBACK = 'http://127.0.0.1:18765'
CHANNELS = ('brew', 'scoop', 'winget')


def registration():
    return fixture('patronus', 'native-registration')


def snapshot():
    return fixture('patronus', 'snapshot')


def fixture_assets(reg):
    """Synthetic release bytes whose digests the fixture snapshot records."""
    return {name: ('synthetic:' + name).encode() for name in reg['configuration']['assets'].values()}


def tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(Path(root).rglob('*')) if path.is_file()}


def channel_tree(root, kind, channel):
    base = Path(root) / kind / channel
    return {path.relative_to(base).as_posix(): path.read_bytes()
            for path in sorted(base.rglob('*')) if path.is_file()}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = Path(self.directory.name) / 'out'


class StageCandidates(Workspace):
    def test_canonical_candidates_are_renderer_bytes_and_release_staging_is_identical(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        result = stage_candidates(self.out, reg, snap)
        for channel in CHANNELS:
            expected = render(channel, reg, snap)
            self.assertEqual(channel_tree(self.out, 'canonical', channel), expected)
            self.assertEqual(channel_tree(self.out, 'staging', channel), expected)
            self.assertEqual(result['candidates'][channel], file_digest(expected))
        self.assertIsNone(result['loopback'])
        self.assertIsNone(result['expect_failure'])

    def test_loopback_staging_changes_only_download_urls(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        assets = fixture_assets(reg)
        result = stage_candidates(self.out, reg, snap, loopback=LOOPBACK, assets=assets)
        self.assertEqual(result['loopback'], LOOPBACK)
        for channel in CHANNELS:
            self.assertEqual(channel_tree(self.out, 'canonical', channel), render(channel, reg, snap))
            self.assertEqual(result['candidates'][channel], file_digest(render(channel, reg, snap)))
        # Scoop: only architecture urls move to loopback; every other field and hash is kept.
        (path, canonical), = render('scoop', reg, snap).items()
        staged = json.loads(channel_tree(self.out, 'staging', 'scoop')[path])
        expected = json.loads(canonical)
        for entry in expected['architecture'].values():
            entry['url'] = LOOPBACK + '/' + entry['url'].rsplit('/', 1)[1]
        self.assertEqual(staged, expected)
        # WinGet: version and locale files are byte-identical; the installer changes only InstallerUrl.
        canonical = render('winget', reg, snap)
        staged = channel_tree(self.out, 'staging', 'winget')
        self.assertEqual(set(staged), set(canonical))
        for path, data in canonical.items():
            if path.endswith('.installer.yaml'):
                self.assertTrue(staged[path].startswith(data.splitlines(keepends=True)[0]))
                document = yaml.safe_load(data)
                for entry in document['Installers']:
                    entry['InstallerUrl'] = LOOPBACK + '/' + entry['InstallerUrl'].rsplit('/', 1)[1]
                self.assertEqual(yaml.safe_load(staged[path]), document)
            else:
                self.assertEqual(staged[path], data)
        # Brew build input: only the four url lines differ, each now a loopback asset URL.
        (path, canonical), = render('brew', reg, snap).items()
        staged = channel_tree(self.out, 'staging', 'brew')[path]
        old, new = canonical.decode().split('\n'), staged.decode().split('\n')
        self.assertEqual(len(old), len(new))
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(len(changed), 4)
        for before, after in changed:
            self.assertTrue(before.lstrip().startswith('url "https://github.com/darkquasar/patronus/'))
            asset = before.split('"')[1].rsplit('/', 1)[1]
            self.assertEqual(after, before.replace(before.split('"')[1], f'{LOOPBACK}/{asset}'))
        # Assets are served from the output tree with their snapshot bytes.
        self.assertEqual(channel_tree(self.out, '.', 'assets'), assets)

    def test_inventory_lists_every_output_with_its_sha256(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        result = stage_candidates(self.out, reg, snap, loopback=LOOPBACK, assets=fixture_assets(reg))
        written = tree(self.out)
        manifest = written.pop('prepare.json')
        self.assertEqual(json.loads(manifest), result)
        self.assertEqual(result['files'], {path: hashlib.sha256(data).hexdigest() for path, data in written.items()})
        self.assertEqual(json.loads(written['snapshot.json']), snap)
        self.assertEqual(json.loads(written['registration.json']), reg)
        self.assertEqual((result['app'], result['version'], result['snapshot_digest'], result['configuration_digest']),
                         ('patronus', '2.7.1', snap['snapshot_digest'], reg['configuration_digest']))
        self.assertEqual({channel: result['validation'][channel]['passed'] for channel in ('scoop', 'winget')},
                         {'scoop': True, 'winget': True})
        self.assertEqual(result['validation']['scoop']['candidate_digest'], result['candidates']['scoop'])

    def test_non_loopback_staging_url_is_rejected(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        for url in ['https://example.com', 'http://192.168.1.2:18765', 'http://127.0.0.1:18765/"x', 'ftp://127.0.0.1']:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'loopback'):
                    stage_candidates(Path(directory) / 'out', reg, snap, loopback=url, assets=fixture_assets(reg))

    def test_fixture_assets_must_match_the_snapshot_and_come_with_loopback(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        changed = fixture_assets(reg)
        changed['patronus-windows-arm64.exe'] += b'x'
        missing = fixture_assets(reg)
        del missing['patronus-linux-amd64']
        cases = [(dict(loopback=LOOPBACK, assets=changed), 'patronus-windows-arm64.exe'),
                 (dict(loopback=LOOPBACK, assets=missing), 'patronus-linux-amd64'),
                 (dict(loopback=LOOPBACK), 'assets'), (dict(assets=fixture_assets(reg)), 'loopback')]
        for options, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, message):
                    stage_candidates(Path(directory) / 'out', reg, snap, **options)

    def test_existing_output_directory_is_refused(self):
        from distribution.native.prepare import stage_candidates
        self.out.mkdir()
        with self.assertRaises(FileExistsError):
            stage_candidates(self.out, registration(), snapshot())

    def test_record_reference_must_locate_this_snapshot(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        ref = dict(schema_version=1, app='patronus', version='2.7.1', controller_sha='c' * 40,
                   configuration_digest=reg['configuration_digest'], records_commit='d' * 40,
                   snapshot_digest=snap['snapshot_digest'])
        stage_candidates(self.out, reg, snap, record_ref=ref)
        for field, value in [('snapshot_digest', 'e' * 64), ('version', '2.7.0'), ('app', 'lumen'),
                             ('configuration_digest', 'f' * 64)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'record'):
                    stage_candidates(Path(directory) / 'out', reg, snap, record_ref=dict(ref, **{field: value}))

    def test_wrong_architecture_negative_swaps_only_staging_mappings(self):
        from distribution.native.prepare import stage_candidates
        reg, snap = registration(), snapshot()
        result = stage_candidates(self.out, reg, snap, loopback=LOOPBACK, assets=fixture_assets(reg),
                                  expect_failure='arch')
        self.assertEqual(result['expect_failure'], 'arch')
        for channel in CHANNELS:
            self.assertEqual(channel_tree(self.out, 'canonical', channel), render(channel, reg, snap))
        (path, canonical), = render('scoop', reg, snap).items()
        staged = json.loads(channel_tree(self.out, 'staging', 'scoop')[path])['architecture']
        original = json.loads(canonical)['architecture']
        for native, other in [('64bit', 'arm64'), ('arm64', '64bit')]:
            entry = dict(original[other], url=LOOPBACK + '/' + original[other]['url'].rsplit('/', 1)[1])
            self.assertEqual(staged[native], entry)
        installer = next(p for p in render('winget', reg, snap) if p.endswith('.installer.yaml'))
        staged = {e['Architecture']: e for e in yaml.safe_load(channel_tree(self.out, 'staging', 'winget')[installer])['Installers']}
        original = {e['Architecture']: e for e in yaml.safe_load(render('winget', reg, snap)[installer])['Installers']}
        for native, other in [('x64', 'arm64'), ('arm64', 'x64')]:
            self.assertEqual(staged[native]['InstallerSha256'], original[other]['InstallerSha256'])
            self.assertEqual(staged[native]['InstallerUrl'], LOOPBACK + '/' + original[other]['InstallerUrl'].rsplit('/', 1)[1])
        (path, canonical), = render('brew', reg, snap).items()
        staged = channel_tree(self.out, 'staging', 'brew')[path].decode()
        identity = snap['payload']['identity']['assets']
        for system in ['darwin', 'linux']:
            amd, arm = f'patronus-{system}-amd64', f'patronus-{system}-arm64'
            intel_block = staged.split(f"on_{'macos' if system == 'darwin' else 'linux'} do")[1].split('on_arm do')[0]
            self.assertIn(f'{LOOPBACK}/{arm}', intel_block)
            self.assertIn(identity[arm]['sha256'], intel_block)
            self.assertNotIn(amd, intel_block)
        install = staged.split('def install')[1]
        self.assertLess(install.index('patronus-darwin-arm64'), install.index('patronus-darwin-amd64'))
        for options in [dict(expect_failure='arch'), dict(loopback=LOOPBACK, assets=fixture_assets(reg), expect_failure='os')]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'expect_failure'):
                    stage_candidates(Path(directory) / 'out', reg, snap, **options)


class FixtureSnapshot(unittest.TestCase):
    def test_fixture_snapshot_freezes_exactly_the_given_bytes(self):
        from distribution.native.prepare import fixture_snapshot
        from distribution.snapshot import validate_snapshot
        reg = registration()
        assets = {name: f'patronus version v2.7.0 {name}'.encode() for name in reg['configuration']['assets'].values()}
        snap = fixture_snapshot(reg, '2.7.0', assets)
        self.assertEqual(validate_snapshot(reg, snap), snap)
        identity = snap['payload']['identity']
        self.assertEqual((identity['app'], identity['version'], identity['tag'], identity['repository']),
                         ('patronus', '2.7.0', 'v2.7.0', 'darkquasar/patronus'))
        self.assertEqual(identity['assets'], {name: dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                                              for name, data in assets.items()})
        checksums = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(assets.items()))
        self.assertEqual(identity['checksums_sha256'], hashlib.sha256(checksums.encode()).hexdigest())
        authorization = snap['payload']['authorization']
        self.assertEqual(authorization['kind'], 'baseline')
        self.assertTrue(authorization['reference'].startswith('fixture-only:'))
        self.assertEqual(snap, fixture_snapshot(reg, '2.7.0', assets))
        with self.assertRaisesRegex(ValueError, 'asset'):
            fixture_snapshot(reg, '2.7.0', dict(list(assets.items())[1:]))


class StageBottles(Workspace):
    BOTTLE_LOOPBACK = 'http://127.0.0.1:18766'

    def test_bottled_candidate_is_canonical_and_staging_changes_only_root_url(self):
        from distribution.bottles import bottled_formula_identity, staging_formula
        from distribution.native.prepare import stage_bottles
        from test_bottles import bottle_kit
        reg, snap = registration(), snapshot()
        archives, _verified, bottles = bottle_kit(reg, snap)
        result = stage_bottles(self.out, reg, snap, archives, source='fresh', loopback=self.BOTTLE_LOOPBACK)
        canonical = render('brew', reg, snap, bottles)
        self.assertEqual(channel_tree(self.out, 'canonical', 'brew'), canonical)
        (path, data), = canonical.items()
        self.assertEqual(channel_tree(self.out, 'staging', 'brew'), {path: staging_formula(data, self.BOTTLE_LOOPBACK)})
        self.assertEqual(channel_tree(self.out, '.', 'bottles'), archives)
        self.assertEqual(result['bottle_set'], bottles)
        self.assertEqual(result['identity'], bottled_formula_identity(reg, snap, bottles))
        self.assertEqual(result['source'], 'fresh')
        self.assertEqual(result['loopback'], self.BOTTLE_LOOPBACK)
        written = tree(self.out)
        self.assertEqual(json.loads(written.pop('brew.json')), result)
        self.assertEqual(result['files'], {p: hashlib.sha256(d).hexdigest() for p, d in written.items()})
        self.assertEqual(sorted(result['verified']), sorted(archives))

    def test_substituted_wrong_platform_or_missing_bottle_is_rejected(self):
        from distribution.bottles import BottleConflict, BottleInvalid
        from distribution.native.prepare import stage_bottles
        from test_bottles import archive, bottle_kit, members
        reg, snap = registration(), snapshot()
        archives, _verified, _bottles = bottle_kit(reg, snap)
        name = next(n for n in archives if 'arm64_sequoia' in n)
        substituted = dict(archives, **{name: archive(members('patronus', '2.7.1', b'rebuilt'))})
        wrong = dict(archives, **{name: archive(members('patronus', '2.7.1', b'synthetic:patronus-darwin-amd64'))})
        incomplete = {n: d for n, d in archives.items() if n != name}
        foreign = dict(incomplete, **{'patronus-2.7.1.monterey_x.bottle.tar.gz': archives[name]})
        for label, case, error in [('substituted', substituted, BottleConflict), ('wrong', wrong, BottleConflict),
                                   ('incomplete', incomplete, BottleInvalid), ('foreign', foreign, BottleConflict)]:
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(error):
                    stage_bottles(Path(directory) / 'out', reg, snap, case, source='fresh', loopback=self.BOTTLE_LOOPBACK)
        with self.assertRaisesRegex(ValueError, 'source'):
            stage_bottles(self.out, reg, snap, archives, source='rebuilt', loopback=self.BOTTLE_LOOPBACK)
        with self.assertRaisesRegex(ValueError, 'loopback'):
            stage_bottles(self.out, reg, snap, archives, source='fresh', loopback='https://example.com')


BREW_PLATFORMS = ('darwin-arm64', 'darwin-amd64', 'linux-amd64', 'linux-arm64')
WINDOWS_PLATFORMS = ('windows-amd64', 'windows-arm64')
PASSED = dict(validate='passed', install='passed', version='passed', uninstall='passed')


def report(manager, platform, inputs, *, stage='check', outcome='passed', reason='', steps=PASSED):
    return dict(schema_version=1, stage=stage, manager=manager, platform=platform, outcome=outcome,
                reason=reason, runner='hosted-' + platform, image='image-' + platform,
                image_version='20261117.1', manager_version=manager + ' 1.0', steps=dict(steps),
                inputs_sha256=hashlib.sha256(inputs).hexdigest())


class SummaryInputs(Workspace):
    def setUp(self):
        super().setUp()
        from distribution.native.prepare import stage_bottles, stage_candidates
        from test_bottles import bottle_kit
        self.reg, self.snap = registration(), snapshot()
        stage_candidates(self.out / 'prepare', self.reg, self.snap, loopback=LOOPBACK, assets=fixture_assets(self.reg))
        archives, _verified, self.bottles = bottle_kit(self.reg, self.snap)
        stage_bottles(self.out / 'brew', self.reg, self.snap, archives, source='fresh', loopback=LOOPBACK)
        self.prepared = (self.out / 'prepare' / 'prepare.json').read_bytes()
        self.brew = (self.out / 'brew' / 'brew.json').read_bytes()
        self.reports = ([report('brew', p, self.brew) for p in BREW_PLATFORMS]
                        + [report(m, p, self.prepared) for m in ('scoop', 'winget') for p in WINDOWS_PLATFORMS])

    def summarize(self, reports=None, brew='default', **options):
        from distribution.native.prepare import summarize
        return summarize(self.prepared, self.brew if brew == 'default' else brew,
                         self.reports if reports is None else reports, run_id=4242, attempt=2, **options)

    def failures(self, document):
        from distribution.bottles import native_failures
        prepared = json.loads(self.prepared)
        formula = json.loads(self.brew)['identity']['formula_sha256']
        return dict(brew=native_failures('brew', self.reg, self.snap, document, formula,
                                         dict(run_id=4242, attempt=2), self.bottles),
                    scoop=native_failures('scoop', self.reg, self.snap, document, prepared['candidates']['scoop']),
                    winget=native_failures('winget', self.reg, self.snap, document, prepared['candidates']['winget']))

    def entry(self, document, manager, platform):
        return next(e for e in document['results'] if (e['manager'], e['platform']) == (manager, platform))


class Summarize(SummaryInputs):
    def test_passing_reports_produce_native_checks_that_open_every_gate(self):
        from distribution.bottles import validate_native_checks
        document = self.summarize()
        self.assertEqual(validate_native_checks(document), document)
        self.assertEqual(self.failures(document), dict(brew=[], scoop=[], winget=[]))
        self.assertEqual(document['run'], dict(id=4242, attempt=2,
                         url='https://github.com/darkquasar/homebrew-tap/actions/runs/4242/attempts/2'))
        self.assertEqual(document['brew']['bottle_set'], self.bottles)
        self.assertEqual(document['brew']['source'], 'fresh')
        self.assertEqual(len(document['results']), 8)
        brew = self.entry(document, 'brew', 'darwin-arm64')
        self.assertEqual((brew['runner'], brew['image'], brew['image_version'], brew['manager_version']),
                         ('hosted-darwin-arm64', 'image-darwin-arm64', '20261117.1', 'brew 1.0'))

    def test_missing_platform_result_fails_only_its_channel(self):
        reports = [r for r in self.reports if (r['manager'], r['platform']) != ('winget', 'windows-arm64')]
        document = self.summarize(reports)
        missing = self.entry(document, 'winget', 'windows-arm64')
        self.assertEqual((missing['outcome'], missing['reason']), ('failed', 'no result'))
        self.assertEqual(set(missing['steps'].values()), {'not-run'})
        failures = self.failures(document)
        self.assertEqual((failures['brew'], failures['scoop']), ([], []))
        self.assertEqual(failures['winget'], ['windows-arm64: failed (no result)'])

    def test_report_that_checked_other_inputs_is_a_failure(self):
        reports = [dict(r, inputs_sha256='0' * 64) if r['platform'] == 'linux-arm64' else r for r in self.reports]
        document = self.summarize(reports)
        entry = self.entry(document, 'brew', 'linux-arm64')
        self.assertEqual(entry['outcome'], 'failed')
        self.assertIn('different inputs', entry['reason'])
        self.assertEqual(self.failures(document)['brew'], [f"linux-arm64: failed ({entry['reason']})"])

    def test_statically_mapped_platform_is_recorded_not_tested(self):
        reports = [r for r in self.reports if r['platform'] != 'windows-arm64']
        document = self.summarize(reports, static={'windows-arm64': 'no-hosted-runner'})
        for manager in ('scoop', 'winget'):
            entry = self.entry(document, manager, 'windows-arm64')
            self.assertEqual((entry['outcome'], entry['reason']), ('statically-mapped', 'no-hosted-runner'))
            self.assertEqual(set(entry['steps'].values()), {'not-run'})
        self.assertEqual(self.failures(document), dict(brew=[], scoop=[], winget=[]))
        ran = self.summarize(static={'windows-arm64': 'no-hosted-runner'})
        self.assertEqual(self.entry(ran, 'scoop', 'windows-arm64')['outcome'], 'passed')

    def test_conflicted_selection_records_brew_null_and_fails_brew_only(self):
        from distribution.bottles import validate_native_checks
        reports = [r for r in self.reports if r['manager'] != 'brew']
        document = self.summarize(reports, brew=None, brew_failure='bottle-conflict: published release incomplete')
        self.assertIsNone(document['brew'])
        self.assertEqual(validate_native_checks(document), document)
        for platform in BREW_PLATFORMS:
            entry = self.entry(document, 'brew', platform)
            self.assertEqual((entry['outcome'], entry['reason']),
                             ('failed', 'bottle-conflict: published release incomplete'))
            self.assertEqual(entry['binding'], json.loads(self.prepared)['candidates']['brew'])
        failures = self.failures(document)
        self.assertEqual(failures['brew'], ['brew evidence missing'])
        self.assertEqual((failures['scoop'], failures['winget']), ([], []))

    def test_build_failure_is_the_result_when_no_check_ran(self):
        failed = dict(PASSED, validate='failed', install='not-run', version='not-run', uninstall='not-run')
        build = report('brew', 'darwin-amd64', self.prepared, stage='build', outcome='failed',
                       reason='architecture-mismatch: executable is the darwin-arm64 asset', steps=failed)
        # Without brew.json (assemble could not run) no brew-check runs either.
        reports = [r for r in self.reports if r['manager'] != 'brew'] + [build]
        document = self.summarize(reports, brew=None)
        entry = self.entry(document, 'brew', 'darwin-amd64')
        self.assertEqual((entry['outcome'], entry['reason'], entry['steps']), ('failed', build['reason'], failed))
        other = self.entry(document, 'brew', 'darwin-arm64')
        self.assertEqual((other['outcome'], other['reason']), ('failed', 'no result'))

    def test_malformed_or_duplicate_reports_are_not_evidence(self):
        bad_passed = dict(report('scoop', 'windows-amd64', self.prepared), steps=dict(PASSED, uninstall='failed'))
        unknown = dict(report('winget', 'windows-amd64', self.prepared), extra=True)
        reports = [r for r in self.reports if r['platform'] != 'windows-amd64'] + [bad_passed, unknown]
        reports += [report('brew', 'linux-amd64', self.brew)]
        document = self.summarize(reports)
        for manager in ('scoop', 'winget'):
            self.assertEqual(self.entry(document, manager, 'windows-amd64')['outcome'], 'failed')
        self.assertEqual(self.entry(document, 'brew', 'linux-amd64')['reason'], 'duplicate results')

    def test_brew_evidence_for_another_snapshot_is_refused(self):
        changed = json.loads(self.brew)
        changed['snapshot_digest'] = 'e' * 64
        with self.assertRaisesRegex(ValueError, 'brew'):
            self.summarize(brew=json.dumps(changed).encode())


class ExpectedFailure(SummaryInputs):
    def failing(self, reason, managers=('brew', 'scoop', 'winget')):
        steps = dict(PASSED, version='failed', uninstall='not-run')
        return [dict(r, outcome='failed', reason=reason, steps=steps) if r['manager'] in managers else r
                for r in self.reports]

    def test_negative_passes_only_when_every_check_fails_with_the_matching_diagnostic(self):
        from distribution.native.prepare import expected_failure
        version = self.summarize(self.failing('version-mismatch: expected v2.7.1, got patronus version v2.7.0'))
        self.assertEqual(expected_failure(version, 'version', self.reg, self.snap), [])
        self.assertNotEqual(expected_failure(version, 'arch', self.reg, self.snap), [])
        arch = self.summarize(self.failing('architecture-mismatch: resolved executable is the windows-arm64 asset'))
        self.assertEqual(expected_failure(arch, 'arch', self.reg, self.snap), [])
        partial = self.summarize(self.failing('version-mismatch: expected v2.7.1', managers=('brew', 'scoop')))
        problems = expected_failure(partial, 'version', self.reg, self.snap)
        self.assertTrue(any('winget' in problem for problem in problems))
        unrelated = self.summarize(self.failing('install failed: network'))
        self.assertNotEqual(expected_failure(unrelated, 'version', self.reg, self.snap), [])
        with self.assertRaisesRegex(ValueError, 'expect_failure'):
            expected_failure(version, 'os', self.reg, self.snap)

    def test_build_stage_architecture_rejection_counts_for_brew(self):
        from distribution.native.prepare import expected_failure
        steps = dict(validate='failed', install='not-run', version='not-run', uninstall='not-run')
        builds = [report('brew', p, self.prepared, stage='build', outcome='failed', steps=steps,
                         reason='architecture-mismatch: executable patronus/2.7.1/bin/patronus is the other asset')
                  for p in BREW_PLATFORMS]
        windows = [r for r in self.failing('architecture-mismatch: hash differs') if r['manager'] != 'brew']
        document = self.summarize(windows + builds, brew=None)
        self.assertEqual(expected_failure(document, 'arch', self.reg, self.snap), [])


class CommandLine(SummaryInputs):
    def binaries(self, versions=('2.7.0', '2.7.1')):
        root = Path(self.directory.name) / 'binaries'
        for version in versions:
            for name in self.reg['configuration']['assets'].values():
                target = root / version / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f'patronus version v{version} {name}'.encode())
        return root

    def test_fixture_command_freezes_the_requested_bytes_under_the_checked_version(self):
        from distribution.native.prepare import main
        binaries = self.binaries()
        out = Path(self.directory.name) / 'negative'
        code = main(['fixture', '--binaries', str(binaries), '--version', '2.7.1', '--assets-version', '2.7.0',
                     '--loopback', LOOPBACK, '--expect-failure', 'version', '--out', str(out)])
        self.assertEqual(code, 0)
        result = json.loads((out / 'prepare.json').read_bytes())
        self.assertEqual((result['version'], result['expect_failure']), ('2.7.1', 'version'))
        identity = json.loads((out / 'snapshot.json').read_bytes())['payload']['identity']
        for name, entry in identity['assets'].items():
            self.assertEqual(entry['sha256'], hashlib.sha256((binaries / '2.7.0' / name).read_bytes()).hexdigest())
            self.assertEqual((out / 'assets' / name).read_bytes(), (binaries / '2.7.0' / name).read_bytes())
        self.assertEqual(json.loads((out / 'registration.json').read_bytes()), self.reg)
        plain = Path(self.directory.name) / 'plain'
        self.assertEqual(main(['fixture', '--binaries', str(binaries), '--version', '2.7.1',
                               '--loopback', LOOPBACK, '--out', str(plain)]), 0)
        identity = json.loads((plain / 'snapshot.json').read_bytes())['payload']['identity']
        self.assertEqual(identity['assets']['patronus-linux-arm64']['sha256'],
                         hashlib.sha256((binaries / '2.7.1' / 'patronus-linux-arm64').read_bytes()).hexdigest())

    def test_summary_command_reads_downloaded_reports_and_enforces_negatives(self):
        from distribution.native.prepare import main
        reports = Path(self.directory.name) / 'reports'
        for index, document in enumerate(self.reports):
            target = reports / f"result-{document['manager']}-{document['platform']}" / f'report-{index}.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document))
        (reports / 'notes.txt').write_text('ignored')
        pins = Path(self.directory.name) / 'pins.json'
        pins.write_text(json.dumps({'statically_mapped': {}}))
        out = Path(self.directory.name) / 'native-checks.json'
        arguments = ['summary', '--prepare', str(self.out / 'prepare' / 'prepare.json'),
                     '--brew', str(self.out / 'brew' / 'brew.json'), '--reports', str(reports),
                     '--pins', str(pins), '--run-id', '4242', '--attempt', '2', '--out', str(out)]
        self.assertEqual(main(arguments), 0)
        document = json.loads(out.read_bytes())
        self.assertEqual(self.failures(document), dict(brew=[], scoop=[], winget=[]))
        out.unlink()
        self.assertEqual(main(arguments + ['--expect-failure', 'version']), 1)
        self.assertTrue(out.exists())
        out.unlink()
        no_brew = [a for a in arguments if a != str(self.out / 'brew' / 'brew.json')]
        no_brew[no_brew.index('--brew') + 1:no_brew.index('--brew') + 1] = ['']
        self.assertEqual(main(no_brew + ['--brew-failure', 'bottle-conflict: x']), 0)
        self.assertIsNone(json.loads(out.read_bytes())['brew'])

    def test_bottles_command_takes_only_this_versions_archives(self):
        from distribution.native.prepare import main
        from test_bottles import bottle_kit
        archives, _verified, bottles = bottle_kit(self.reg, self.snap)
        built = Path(self.directory.name) / 'built'
        for index, (name, data) in enumerate(archives.items()):
            (built / f'bottle-{index}' / 'bottles').mkdir(parents=True)
            (built / f'bottle-{index}' / 'bottles' / name).write_bytes(data)
            (built / f'bottle-{index}' / 'bottles' / name.replace('2.7.1', '2.7.0')).write_bytes(b'older version')
        prepare = self.out / 'prepare'
        out = Path(self.directory.name) / 'staged'
        self.assertEqual(main(['bottles', '--prepare', str(prepare), '--archives', str(built), '--source', 'fresh',
                               '--loopback', LOOPBACK, '--out', str(out)]), 0)
        self.assertEqual(json.loads((out / 'brew.json').read_bytes())['bottle_set'], bottles)
        self.assertEqual(channel_tree(out, '.', 'bottles'), archives)


PRESERVATION = Path(__file__).parent / 'native' / 'preservation'


class Preservation(Workspace):
    def roots(self):
        project, home = Path(self.directory.name) / 'project', Path(self.directory.name) / 'home'
        project.mkdir()
        home.mkdir()
        (home / '.cache').mkdir()
        (home / '.cache' / 'manager.log').write_text('manager-owned')
        return project, home

    def test_seeded_sentinels_match_the_manifest_and_changes_are_detected(self):
        from distribution.native.prepare import preservation_inventory, seed_preservation
        manifest = json.loads((PRESERVATION / 'manifest.json').read_text())['files']
        project, home = self.roots()
        self.assertEqual(seed_preservation(PRESERVATION, project, home), manifest)
        self.assertEqual(preservation_inventory(PRESERVATION, project, home), manifest)
        (home / '.cache' / 'other.log').write_text('manager-owned, not a preservation target')
        self.assertEqual(preservation_inventory(PRESERVATION, project, home), manifest)
        (home / '.claude' / 'extra.json').write_text('{}')
        (project / '.patronus' / 'state.json').write_text('{}')
        (home / '.pi' / 'agent' / 'AGENTS.md').unlink()
        changed = preservation_inventory(PRESERVATION, project, home)
        self.assertIn('home/.claude/extra.json', changed)
        self.assertNotEqual(changed['project/.patronus/state.json'], manifest['project/.patronus/state.json'])
        self.assertIsNone(changed['home/.pi/agent/AGENTS.md'])
        (project / 'new.txt').write_text('added')
        self.assertIn('project/new.txt', preservation_inventory(PRESERVATION, project, home))

    def test_seeding_refuses_to_overlay_existing_targets(self):
        from distribution.native.prepare import seed_preservation
        project, home = self.roots()
        (home / 'unrelated.txt').write_text('a real user file')
        with self.assertRaisesRegex(ValueError, 'unrelated.txt'):
            seed_preservation(PRESERVATION, project, home)
        self.assertEqual((home / 'unrelated.txt').read_text(), 'a real user file')


class NativeNames(Workspace):
    def test_names_for_a_second_app_come_from_its_registration(self):
        from distribution.native.prepare import native_names, stage_candidates
        lumen = fixture('lumen', 'native-registration')
        names = native_names(lumen, '2.7.1')
        self.assertEqual(names, dict(
            app='lumen', tap='darkquasar/tap', formula='lumen', formula_path='Formula/lumen.rb',
            scoop_bucket='darkquasar', scoop_package='lumen', scoop_path='bucket/lumen.json',
            winget_identifier='darkquasar.Lumen', winget_path='manifests/d/darkquasar/Lumen/2.7.1',
            command='lumen', assets=lumen['configuration']['assets']))
        self.assertEqual(native_names(registration(), '2.7.1')['formula_path'], 'Formula/patronus.rb')
        from distribution.native.prepare import fixture_snapshot
        assets = {name: name.encode() for name in lumen['configuration']['assets'].values()}
        result = stage_candidates(self.out, lumen, fixture_snapshot(lumen, '2.7.1', assets),
                                  loopback=LOOPBACK, assets=assets)
        self.assertEqual(result['names'], names)
        for channel, path in [('brew', 'Formula/lumen.rb'), ('scoop', 'bucket/lumen.json'),
                              ('winget', 'manifests/d/darkquasar/Lumen/2.7.1/darkquasar.Lumen.installer.yaml')]:
            self.assertTrue((self.out / 'staging' / channel / path).is_file(), path)

    def test_package_identity_and_command_are_separate_fields(self):
        from distribution.native.prepare import native_names
        names = native_names(registration(), '2.7.1')
        self.assertEqual({k: names[k] for k in ['formula', 'scoop_package', 'winget_identifier', 'command']},
                         dict(formula='patronus', scoop_package='patronus',
                              winget_identifier='darkquasar.Patronus', command='patronus'))


class VersionToken(unittest.TestCase):
    def test_reported_version_must_be_an_exact_token(self):
        from distribution.native.prepare import version_matches
        self.assertTrue(version_matches('patronus version v2.7.1', '2.7.1'))
        self.assertTrue(version_matches('patronus version v2.7.1\n', '2.7.1'))
        for output in ['patronus version v2.7.10', 'patronus version v2.7.1-rc1', 'patronus version v2.7.0',
                       'patronus version v2.7.1+build', 'patronus version v12.7.1', 'patronus version 2.7.1',
                       'patronus version v2.7.1.1', 'patronus version v2x7x1', '']:
            with self.subTest(output=output):
                self.assertFalse(version_matches(output, '2.7.1'))

    def test_version_check_command_reports_the_mismatch(self):
        import contextlib, io
        from distribution.native.prepare import main
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['version-check', '--version', '2.7.1', '--output', 'patronus version v2.7.1']), 0)
            self.assertEqual(main(['version-check', '--version', '2.7.1', '--output', 'patronus version v2.7.10']), 1)
        self.assertIn('version-mismatch: expected v2.7.1, got: patronus version v2.7.10', out.getvalue())


class MissingSelection(SummaryInputs):
    def test_missing_selection_fails_only_brew_and_keeps_windows_evidence(self):
        from distribution.bottles import native_failures, validate_native_checks
        from distribution.native.prepare import main
        reports = Path(self.directory.name) / 'reports'
        reports.mkdir()
        for index, document in enumerate(r for r in self.reports if r['manager'] != 'brew'):
            (reports / f'report-{index}.json').write_text(json.dumps(document))
        pins = Path(self.directory.name) / 'pins.json'
        pins.write_text(json.dumps({'statically_mapped': {}}))
        out = Path(self.directory.name) / 'native-checks.json'
        code = main(['summary', '--prepare', str(self.out / 'prepare' / 'prepare.json'),
                     '--selection', str(Path(self.directory.name) / 'absent' / 'selection.json'),
                     '--brew', str(self.out / 'brew' / 'brew.json'), '--reports', str(reports),
                     '--pins', str(pins), '--run-id', '4242', '--attempt', '1', '--out', str(out)])
        self.assertEqual(code, 0)
        document = validate_native_checks(json.loads(out.read_bytes()))
        self.assertIsNone(document['brew'])
        for entry in document['results']:
            if entry['manager'] == 'brew':
                self.assertEqual(entry['outcome'], 'failed')
                self.assertTrue(entry['reason'].startswith('bottle-selection-unavailable:'), entry['reason'])
            else:
                self.assertEqual(entry['outcome'], 'passed')
        prepared = json.loads(self.prepared)
        context = dict(run_id=4242, attempt=1)
        self.assertEqual(native_failures('scoop', self.reg, self.snap, document, prepared['candidates']['scoop'], context), [])
        self.assertEqual(native_failures('winget', self.reg, self.snap, document, prepared['candidates']['winget'], context), [])
        self.assertEqual(native_failures('brew', self.reg, self.snap, document, None, context, None),
                         ['brew evidence missing'])

    def test_selection_conflict_and_missing_assembly_reach_brew_results(self):
        from distribution.native.prepare import main
        pins = Path(self.directory.name) / 'pins.json'
        pins.write_text(json.dumps({'statically_mapped': {}}))
        selection = Path(self.directory.name) / 'selection.json'
        out = Path(self.directory.name) / 'native-checks.json'
        base = ['summary', '--prepare', str(self.out / 'prepare' / 'prepare.json'), '--selection', str(selection),
                '--reports', str(Path(self.directory.name)), '--pins', str(pins), '--run-id', '4242',
                '--attempt', '1', '--out', str(out)]
        selection.write_text(json.dumps(dict(source='conflict', reason='bottle-conflict: incomplete release')))
        self.assertEqual(main(base + ['--brew', str(self.out / 'brew' / 'brew.json')]), 0)
        document = json.loads(out.read_bytes())
        self.assertIsNone(document['brew'])
        self.assertEqual({e['reason'] for e in document['results'] if e['manager'] == 'brew'},
                         {'bottle-conflict: incomplete release'})
        selection.write_text(json.dumps(dict(source='fresh', reason='fresh')))
        self.assertEqual(main(base + ['--brew', str(Path(self.directory.name) / 'missing' / 'brew.json')]), 0)
        reasons = {e['reason'] for e in json.loads(out.read_bytes())['results'] if e['manager'] == 'brew'}
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons.pop().startswith('bottle-assembly-failed:'))


if __name__ == '__main__':
    unittest.main()
