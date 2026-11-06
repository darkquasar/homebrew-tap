"""Public rendering contracts using synthetic T1 registrations and snapshots."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

from distribution.validation import render
from distribution.contracts import canonical_digest

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(app, kind):
    return json.loads((FIXTURES / f'{app}-{kind}.json').read_text())


class Renderers(unittest.TestCase):
    def brew_selection(self, candidate, system, cpu):
        # Homebrew's Ruby is supported; macOS system Ruby 2.6 is not.
        ruby = os.environ.get('TEST_RUBY', shutil.which('ruby'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidate.rb'
            path.write_bytes(next(iter(candidate.values())))
            return subprocess.run([ruby, str(FIXTURES / 'brew-probe.rb'), str(path)],
                                  env=dict(os.environ, PROBE_OS=system, PROBE_CPU=cpu),
                                  capture_output=True, text=True, timeout=10)

    def test_macos_arm64_maps_frozen_url_hash_and_command(self):
        registration = fixture('patronus', 'registration')
        snapshot = fixture('patronus', 'snapshot')
        result = render('brew', registration, snapshot)
        self.assertIn('Formula/patronus.rb', result)
        text = result['Formula/patronus.rb'].decode()
        asset = 'patronus-darwin-arm64'
        self.assertIn('https://github.com/darkquasar/patronus/releases/download/v2.7.1/' + asset, text)
        self.assertIn(snapshot['payload']['identity']['assets'][asset]['sha256'], text)
        self.assertIn('bin.install "patronus-darwin-arm64" => "patronus"', text)

    def test_brew_selects_all_four_mappings_and_denies_unsupported_before_install(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            candidate = render('brew', reg, snap)
            for system in ['darwin', 'linux']:
                for cpu in ['amd64', 'arm64']:
                    with self.subTest(app=app, system=system, cpu=cpu):
                        result = self.brew_selection(candidate, system, cpu)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        selected = json.loads(result.stdout)
                        asset = reg['configuration']['assets'][system + '-' + cpu]
                        identity = snap['payload']['identity']
                        self.assertEqual(selected, {
                            'url': f"https://github.com/{identity['repository']}/releases/download/{identity['tag']}/{asset}",
                            'using': 'nounzip', 'sha256': identity['assets'][asset]['sha256'],
                            'bin': {asset: app}})
            for system, cpu in [('darwin', 'x86'), ('linux', 'x86'), ('freebsd', 'arm64'),
                                ('linux', 'riscv64'), ('darwin', 'armv7')]:
                result = self.brew_selection(candidate, system, cpu)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unsupported', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_scoop_two_architectures_use_local_aliases_without_x86_fallback(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            candidate = render('scoop', reg, snap)
            self.assertEqual(set(candidate), {f'bucket/{app}.json'})
            manifest = json.loads(candidate[f'bucket/{app}.json'])
            self.assertEqual(set(manifest), {'version', 'description', 'homepage', 'license', 'architecture'})
            self.assertEqual(set(manifest['architecture']), {'64bit', 'arm64'})
            self.assertEqual(manifest['version'], snap['payload']['identity']['version'])
            for native, cpu in [('64bit', 'amd64'), ('arm64', 'arm64')]:
                asset = reg['configuration']['assets']['windows-' + cpu]
                identity = snap['payload']['identity']
                self.assertEqual(manifest['architecture'][native], {
                    'url': f"https://github.com/{identity['repository']}/releases/download/{identity['tag']}/{asset}",
                    'hash': identity['assets'][asset]['sha256'], 'bin': [[asset, app]]})

    def test_winget_complete_portable_set_maps_x64_and_arm64(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            candidate = render('winget', reg, snap)
            c, identity = reg['configuration'], snap['payload']['identity']
            package = c['identifiers']['winget']
            directory = c['destinations']['winget']['path'] + '/' + identity['version']
            self.assertEqual(set(candidate), {f'{directory}/{package}{suffix}.yaml'
                for suffix in ['', '.installer', '.locale.en-US']})
            docs = {yaml.safe_load(data)['ManifestType']: yaml.safe_load(data)
                    for data in candidate.values()}
            self.assertEqual(set(docs), {'version', 'installer', 'defaultLocale'})
            for doc in docs.values():
                self.assertEqual(doc['PackageIdentifier'], package)
                self.assertEqual(doc['PackageVersion'], identity['version'])
                self.assertEqual(doc['ManifestVersion'], '1.10.0')
            self.assertEqual(docs['version']['DefaultLocale'], 'en-US')
            installer = docs['installer']
            self.assertEqual(installer['InstallerType'], 'portable')
            self.assertEqual(installer['Scope'], 'user')
            self.assertEqual(installer['Commands'], [app])
            self.assertEqual(len(installer['Installers']), 2)
            for entry, native, cpu in zip(installer['Installers'], ['x64', 'arm64'], ['amd64', 'arm64']):
                asset = c['assets']['windows-' + cpu]
                self.assertEqual(entry, {'Architecture': native,
                    'InstallerUrl': f"https://github.com/{identity['repository']}/releases/download/{identity['tag']}/{asset}",
                    'InstallerSha256': identity['assets'][asset]['sha256'].upper()})
            locale = docs['defaultLocale']
            self.assertEqual(locale['PackageLocale'], 'en-US')
            self.assertEqual(locale['Publisher'], c['owner_login'])
            self.assertEqual(locale['PackageName'], package.split('.')[1])
            self.assertEqual(locale['License'], c['license'])
            self.assertEqual(locale['ShortDescription'], c['description'])

    def test_repeated_rendering_is_deterministic_and_does_not_mutate_inputs(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            before = copy.deepcopy((reg, snap))
            for channel in ['brew', 'scoop', 'winget']:
                self.assertEqual(render(channel, reg, snap), render(channel, reg, snap))
            self.assertEqual((reg, snap), before)

    def test_unapproved_or_incomplete_inputs_are_rejected_by_all_renderers(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        bad_reg = copy.deepcopy(reg)
        bad_reg['configuration']['description'] = 'unapproved'
        bad_snap = copy.deepcopy(snap)
        del bad_snap['payload']['identity']['commit']
        for channel in ['brew', 'scoop', 'winget']:
            for r, s in [(bad_reg, snap), (reg, bad_snap), (reg, fixture('lumen', 'snapshot'))]:
                with self.assertRaises(ValueError):
                    render(channel, r, s)
        with self.assertRaisesRegex(ValueError, 'unsupported channel'):
            render('other', reg, snap)

    def test_metadata_is_literal_in_ruby_json_and_yaml(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        hostile = 'text "quoted" \\ #{raise "oops"} #@value #$LOAD_PATH: [yes] café'
        reg['configuration']['description'] = hostile
        digest = canonical_digest(reg['configuration'])
        reg['configuration_digest'] = reg['approval']['configuration_digest'] = digest
        # The identity and its initial approval remain frozen after a presentation correction.
        candidate = render('brew', reg, snap)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'metadata.rb'
            script.write_bytes(next(iter(candidate.values())))
            ruby = os.environ.get('TEST_RUBY', shutil.which('ruby'))
            probe = (FIXTURES / 'brew-probe.rb').read_text()
            probe = probe.replace('def self.desc(value); end', 'def self.desc(value); $description = value; end')
            probe += '\nputs JSON.generate($description)\n'
            harness = Path(directory) / 'probe.rb'
            harness.write_text(probe)
            result = subprocess.run([ruby, str(harness), str(script)], capture_output=True, text=True,
                env=dict(os.environ, PROBE_OS='darwin', PROBE_CPU='arm64'), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.splitlines()[-1]), hostile)
        scoop = json.loads(next(iter(render('scoop', reg, snap).values())))
        self.assertEqual(scoop['description'], hostile)
        winget = render('winget', reg, snap)
        locale = next(yaml.safe_load(data) for path, data in winget.items() if '.locale.' in path)
        self.assertEqual(locale['ShortDescription'], hostile)

    def test_staging_uses_two_independent_repositories_versions_and_frozen_bytes(self):
        identities = []
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            for app in ['patronus', 'lumen']:
                reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
                identity = snap['payload']['identity']
                identities.append((identity['repository'], identity['version']))
                checksums = b''
                for asset in reg['configuration']['assets'].values():
                    downloaded = ('synthetic:' + asset).encode()
                    digest = hashlib.sha256(downloaded).hexdigest()
                    self.assertEqual(identity['assets'][asset], {'size': len(downloaded), 'sha256': digest})
                    checksums += (digest + '  ' + asset + '\n').encode()
                self.assertEqual(hashlib.sha256(checksums).hexdigest(), identity['checksums_sha256'])
                for channel in ['brew', 'scoop', 'winget']:
                    for path, data in render(channel, reg, snap).items():
                        target = staging / channel / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        self.assertFalse(target.exists(), 'apps must never share a native path')
                        target.write_bytes(data)
            self.assertEqual(sorted(p.stem for p in (staging / 'brew/Formula').glob('*.rb')),
                             ['lumen', 'patronus'])
            self.assertEqual(sorted(p.stem for p in (staging / 'scoop/bucket').glob('*.json')),
                             ['lumen', 'patronus'])
        self.assertNotEqual(identities[0][0], identities[1][0])
        self.assertNotEqual(identities[0][1], identities[1][1])


if __name__ == '__main__':
    unittest.main()
