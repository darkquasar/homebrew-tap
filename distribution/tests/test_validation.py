"""Candidates must pass real parsers/schemas and frozen identity checks."""
import hashlib
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import yaml
from unittest.mock import patch
import unittest

from distribution.contracts import canonical_digest
from distribution.tests.test_renderers import fixture
from distribution.validation import render, validate_candidate, validate_directory
import distribution.validation as validation


class Validation(unittest.TestCase):
    @staticmethod
    def native_success(command, **kwargs):
        require_timeout = kwargs.get('timeout')
        assert require_timeout == (300 if '--only-cops' in command else 60)
        output = 'Syntax OK\n'
        if command[-1] == '--version':
            output = 'native tool fixture 1.0\n'
        elif command[-1] == '--repo':
            output = '/fixture/homebrew\n'
        elif command[-1] == 'HEAD':
            output = 'a' * 40 + '\n'
        return subprocess.CompletedProcess(command, 0, output, '')

    def assert_rejected(self, channel, reg, snap, candidate, text=None):
        result = validate_candidate(channel, reg, snap, candidate)
        self.assertFalse(result['passed'], 'invalid candidate accepted')
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['errors'])
        if text:
            self.assertIn(text, ' '.join(result['errors']))
        return result

    def test_scoop_candidate_passes_real_schema_with_complete_byte_bound_evidence(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            candidate = render('scoop', reg, snap)
            result = validate_candidate('scoop', reg, snap, candidate)
            self.assertTrue(result['passed'], result['errors'])
            self.assertEqual(result['state'], 'validated')
            hashes = {path: hashlib.sha256(data).hexdigest() for path, data in candidate.items()}
            self.assertEqual(result['candidate_files'], hashes)
            self.assertEqual(result['candidate_digest'], canonical_digest(hashes))
            self.assertEqual(result['snapshot_digest'], snap['snapshot_digest'])
            self.assertEqual(result['configuration_digest'], reg['configuration_digest'])
            self.assertEqual(result['validation']['dependencies']['PyYAML'], '6.0.3')
            self.assertEqual(result['validation']['dependencies']['jsonschema'], '4.26.0')
            self.assertEqual(result['validation']['schemas']['scoop']['draft'],
                             'http://json-schema.org/draft-07/schema#')
            self.assertEqual(len(result['validation']['schemas']['scoop']['sha256']), 64)

    def test_scoop_denies_schema_valid_identity_mapping_and_lifecycle_changes(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('scoop', reg, snap)
        path = next(iter(candidate))
        original = json.loads(candidate[path])
        mutations = [
            lambda d: d.update(version='2.7.2'),
            lambda d: d['architecture']['64bit'].update(url=d['architecture']['arm64']['url']),
            lambda d: d['architecture']['64bit'].update(hash='a' * 64),
            lambda d: d['architecture']['64bit'].update(bin=[['other.exe', 'patronus']]),
            lambda d: d['architecture'].update({'32bit': d['architecture']['64bit']}),
            lambda d: d.update(url=d['architecture']['64bit']['url']),
            lambda d: d.update(pre_install='patronus install'),
            lambda d: d.update(description='unapproved metadata'),
        ]
        for index, change in enumerate(mutations):
            doc = copy.deepcopy(original)
            change(doc)
            with self.subTest(mutation=index):
                self.assert_rejected('scoop', reg, snap, {path: json.dumps(doc).encode()})
        self.assert_rejected('scoop', reg, snap, {'bucket/lumen.json': candidate[path]}, 'path')
        self.assert_rejected('scoop', reg, snap, {path: b'{"version":"2.7.1","version":"2.7.1"}'}, 'duplicate')
        bad = copy.deepcopy(original)
        del bad['version']
        self.assert_rejected('scoop', reg, snap, {path: json.dumps(bad).encode()})

    def test_winget_candidates_pass_all_three_real_schemas_for_both_apps(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            result = validate_candidate('winget', reg, snap, render('winget', reg, snap))
            self.assertTrue(result['passed'], result['errors'])
            self.assertEqual(result['state'], 'validated')
            self.assertEqual(len(result['candidate_files']), 3)

    def test_portable_metadata_without_scope_validates_and_scope_overrides_are_rejected(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            candidate = render('winget', reg, snap)
            result = validate_candidate('winget', reg, snap, candidate)
            self.assertTrue(result['passed'], result['errors'])
            path = next(p for p in candidate if p.endswith('.installer.yaml'))
            document = yaml.safe_load(candidate[path])
            self.assertNotIn('Scope', document)
            for scope in ['user', 'machine']:
                altered = dict(candidate)
                changed = dict(document, Scope=scope)
                header = candidate[path].splitlines(keepends=True)[0]
                altered[path] = header + yaml.safe_dump(changed, sort_keys=False).encode()
                with self.subTest(app=app, scope=scope):
                    self.assert_rejected('winget', reg, snap, altered)

    def test_winget_rejects_cross_file_identity_and_frozen_mapping_changes(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('winget', reg, snap)
        version = next(p for p in candidate if p.endswith('/darkquasar.Patronus.yaml'))
        installer = next(p for p in candidate if p.endswith('.installer.yaml'))
        locale = next(p for p in candidate if '.locale.' in p)
        cases = [
            (version, lambda d: d.update(PackageVersion='2.7.2')),
            (version, lambda d: d.update(PackageIdentifier='darkquasar.Lumen')),
            (version, lambda d: d.update(DefaultLocale='fr-FR')),
            (locale, lambda d: d.update(PackageLocale='fr-FR')),
            (locale, lambda d: d.pop('Publisher')),
            (locale, lambda d: d.update(ShortDescription='unapproved')),
            (installer, lambda d: d['Installers'][0].update(Architecture='x86')),
            (installer, lambda d: d['Installers'][0].update(InstallerUrl=d['Installers'][1]['InstallerUrl'])),
            (installer, lambda d: d['Installers'][0].update(InstallerSha256='A'*64)),
            (installer, lambda d: d['Installers'].append(d['Installers'][0])),
            (installer, lambda d: d.update(Commands=['other'])),
            (installer, lambda d: d.update(Scope='machine')),
            (installer, lambda d: d.update(InstallerType='exe')),
        ]
        for index, (path, change) in enumerate(cases):
            altered = dict(candidate)
            doc = yaml.safe_load(altered[path])
            change(doc)
            altered[path] = yaml.safe_dump(doc).encode()
            with self.subTest(mutation=index):
                self.assert_rejected('winget', reg, snap, altered)
        for path in candidate:
            incomplete = dict(candidate)
            del incomplete[path]
            self.assert_rejected('winget', reg, snap, incomplete, 'path')
        self.assert_rejected('winget', reg, snap, dict(candidate, **{'../escape.yaml': b'{}'}), 'path')

    def test_duplicate_yaml_keys_unsafe_tags_and_oversized_candidates_fail_closed(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('winget', reg, snap)
        path = next(p for p in candidate if p.endswith('/darkquasar.Patronus.yaml'))
        bad = dict(candidate)
        bad[path] += b'PackageVersion: 2.7.1\n'
        self.assert_rejected('winget', reg, snap, bad, 'duplicate')
        bad[path] = b'!!python/object/apply:os.system ["exit 0"]'
        self.assert_rejected('winget', reg, snap, bad, 'tag')
        bad[path] = b' ' * (1024 * 1024 + 1)
        self.assert_rejected('winget', reg, snap, bad, 'size')

    def test_schema_pin_missing_schema_and_unvendored_reference_are_unavailable(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('scoop', reg, snap)
        for case in ['tamper', 'missing', 'draft', 'reference', 'dependency']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(validation.HERE / 'schemas', root / 'schemas')
                shutil.copy(validation.HERE / 'validation-requirements.txt', root)
                pins = json.loads((validation.HERE / 'validator-pins.json').read_text())
                schema = root / pins['schemas']['scoop']['path']
                if case == 'missing':
                    schema.unlink()
                elif case == 'tamper':
                    schema.write_bytes(schema.read_bytes() + b' ')
                elif case == 'dependency':
                    pins['dependencies']['jsonschema']['version'] = '0.0.0'
                else:
                    doc = json.loads(schema.read_bytes())
                    if case == 'draft':
                        doc['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
                    else:
                        doc['definitions']['unused'] = {'$ref': 'https://example.invalid/remote.json'}
                    raw = json.dumps(doc).encode()
                    schema.write_bytes(raw)
                    pins['schemas']['scoop']['sha256'] = hashlib.sha256(raw).hexdigest()
                (root / 'validator-pins.json').write_text(json.dumps(pins))
                with patch.object(validation, 'HERE', root), patch('socket.create_connection',
                        side_effect=AssertionError('network access forbidden')):
                    result = self.assert_rejected('scoop', reg, snap, candidate)
                self.assertEqual(result['failure_code'], 'validation-unavailable')

    def test_real_format_checker_rejects_malformed_registered_uri(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        reg['configuration']['homepage'] = 'https://[bad'
        digest = canonical_digest(reg['configuration'])
        reg['configuration_digest'] = reg['approval']['configuration_digest'] = digest
        result = self.assert_rejected('scoop', reg, snap, render('scoop', reg, snap))
        self.assertIn('uri', ' '.join(result['errors']))

    def test_brew_validation_requires_native_syntax_and_style_success(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('brew', reg, snap)
        with patch('distribution.validation.subprocess.run', side_effect=self.native_success) as native:
            result = validate_candidate('brew', reg, snap, candidate)
        self.assertTrue(result['passed'], result['errors'])
        commands = [call.args[0] for call in native.call_args_list]
        self.assertTrue(any('-c' in command for command in commands))
        self.assertTrue(any('style' in command for command in commands))
        self.assertEqual(result['validation']['native']['brew_commit'], 'a' * 40)
        for failure in ['exit', 'timeout', 'missing']:
            def run(command, **kwargs):
                if 'style' in command and '--only-cops' not in command:
                    if failure == 'timeout':
                        raise subprocess.TimeoutExpired(command, 60)
                    if failure == 'missing':
                        raise FileNotFoundError('brew unavailable')
                    return subprocess.CompletedProcess(command, 1, 'style error', '')
                return self.native_success(command, **kwargs)
            with self.subTest(failure=failure), patch('distribution.validation.subprocess.run', side_effect=run):
                result = self.assert_rejected('brew', reg, snap, candidate)
            self.assertEqual(result['failure_code'],
                'validation-failed' if failure == 'exit' else 'validation-unavailable')
        bad = {path: data + b'\nsystem("touch /unowned")\n' for path, data in candidate.items()}
        with patch('distribution.validation.subprocess.run', side_effect=AssertionError('must reject before Ruby')):
            self.assert_rejected('brew', reg, snap, bad, 'formula')

    def test_brew_information_command_failures_mean_validation_is_unavailable(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('brew', reg, snap)
        for failed_command in [['brew', '--version'], ['brew', '--repo'],
                               ['git', '-C', '/fixture/homebrew', 'rev-parse', 'HEAD'],
                               ['brew', 'ruby', '--', '--version']]:
            def run(command, **kwargs):
                if command == failed_command:
                    return subprocess.CompletedProcess(command, 1, '', 'tool infrastructure unavailable')
                return self.native_success(command, **kwargs)
            with self.subTest(command=failed_command), patch('distribution.validation.subprocess.run', side_effect=run):
                result = self.assert_rejected('brew', reg, snap, candidate)
            self.assertEqual(result['failure_code'], 'validation-unavailable')

    def test_brew_bootstrap_has_its_own_bound_and_unavailable_failure(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('brew', reg, snap)
        def ready(command, **kwargs):
            if '--only-cops' in command:
                self.assertEqual(kwargs['timeout'], 300)
                self.assertNotEqual(Path(command[-1]).name, 'patronus.rb')
                self.assertTrue(Path(command[-1]).is_file())
                return subprocess.CompletedProcess(command, 0, 'toolchain ready', '')
            return self.native_success(command, **kwargs)
        with patch('distribution.validation.subprocess.run', side_effect=ready) as native:
            result = validate_candidate('brew', reg, snap, candidate)
        self.assertTrue(result['passed'], result['errors'])
        commands = [call.args[0] for call in native.call_args_list]
        self.assertEqual(sum('--only-cops' in command for command in commands), 1)
        warmup = next(i for i, command in enumerate(commands) if '--only-cops' in command)
        check = next(i for i, command in enumerate(commands) if 'style' in command and '--only-cops' not in command)
        self.assertLess(warmup, check)
        def unavailable(command, **kwargs):
            if '--only-cops' in command:
                return subprocess.CompletedProcess(command, 1, '', 'gem bootstrap failed')
            if '-c' in command or 'style' in command:
                self.fail('candidate must not run after failed bootstrap')
            return self.native_success(command, **kwargs)
        with patch('distribution.validation.subprocess.run', side_effect=unavailable):
            result = self.assert_rejected('brew', reg, snap, candidate)
        self.assertEqual(result['failure_code'], 'validation-unavailable')

    def test_brew_checks_do_not_forward_credentials(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        sensitive = dict(GH_TOKEN='fixture-secret', GITHUB_TOKEN='fixture-secret',
            HOMEBREW_GITHUB_API_TOKEN='fixture-secret', RECORDING_APP_PRIVATE_KEY='fixture-secret',
            RUBYOPT='fixture-injection', BUNDLE_GEMFILE='fixture-injection')
        with patch.dict('os.environ', sensitive), patch('distribution.validation.subprocess.run',
                side_effect=self.native_success) as native:
            result = validate_candidate('brew', reg, snap, render('brew', reg, snap))
        self.assertTrue(result['passed'], result['errors'])
        for call in native.call_args_list:
            self.assertTrue(set(sensitive).isdisjoint(call.kwargs['env']))
            self.assertNotIn('fixture-secret', call.kwargs['env'].values())

    def test_brew_syntax_check_uses_the_same_ruby_as_homebrew(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with patch('distribution.validation.subprocess.run', side_effect=self.native_success) as native:
            result = validate_candidate('brew', reg, snap, render('brew', reg, snap))
        self.assertTrue(result['passed'], result['errors'])
        commands = [call.args[0] for call in native.call_args_list]
        self.assertIn(['brew', 'ruby', '--', '--version'], commands)
        syntax = next(command for command in commands if '-c' in command)
        self.assertEqual(syntax[:4], ['brew', 'ruby', '--', '-c'])

    def test_maintainer_cli_uses_real_candidate_bytes_and_reports_denials(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registration, snapshot = root / 'registration.json', root / 'snapshot.json'
            registration.write_text(json.dumps(reg))
            snapshot.write_text(json.dumps(snap))
            staging = root / 'candidate'
            for path, data in render('scoop', reg, snap).items():
                target = staging / path
                target.parent.mkdir(parents=True)
                target.write_bytes(data)
            output = root / 'result.json'
            command = [sys.executable, '-B', '-m', 'distribution.validation', '--channel', 'scoop',
                '--app', 'patronus', '--registration', str(registration), '--snapshot', str(snapshot),
                '--candidate-dir', str(staging), '--out', str(output)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertTrue(output.is_file(), 'CLI must write validation evidence')
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(output.read_bytes())
            self.assertTrue(evidence['passed'], evidence['errors'])
            target.write_bytes(target.read_bytes().replace(b'2.7.1', b'2.7.2'))
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(output.read_bytes())['passed'])
            target.unlink()
            target.symlink_to(registration)
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn('symlink', ' '.join(json.loads(output.read_bytes())['errors']))

    def test_cli_without_third_party_dependencies_still_writes_unavailable_evidence(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registration, snapshot = root / 'registration.json', root / 'snapshot.json'
            registration.write_text(json.dumps(reg))
            snapshot.write_text(json.dumps(snap))
            candidate_dir = root / 'candidate'
            for path, data in render('scoop', reg, snap).items():
                path = candidate_dir / path
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
            out = root / 'result.json'
            args = ['distribution.validation', '--channel', 'scoop', '--app', 'patronus',
                    '--registration', str(registration), '--snapshot', str(snapshot),
                    '--candidate-dir', str(candidate_dir), '--out', str(out)]
            source_root = str(Path(__file__).resolve().parents[2])
            code = (f'import runpy, sys; sys.path.insert(0, {source_root!r}); '
                    f'sys.argv = {args!r}; runpy.run_module("distribution.validation", run_name="__main__")')
            process = subprocess.run([sys.executable, '-B', '-I', '-S', '-c', code],
                                     capture_output=True, text=True, timeout=30)
            self.assertTrue(out.is_file(), process.stderr)
            self.assertEqual(process.returncode, 1)
            result = json.loads(out.read_bytes())
            self.assertFalse(result['passed'])
            self.assertEqual(result['failure_code'], 'validation-unavailable')

    def test_unreadable_directory_result_keeps_identity_and_explicit_missing_byte_evidence(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        with tempfile.TemporaryDirectory() as directory:
            result = validate_directory('scoop', reg, snap, Path(directory) / 'missing')
        self.assertFalse(result['passed'])
        self.assertEqual(result.get('snapshot_digest'), snap['snapshot_digest'])
        self.assertEqual(result.get('configuration_digest'), reg['configuration_digest'])
        self.assertIn('candidate_digest', result)
        self.assertIsNone(result['candidate_digest'])
        self.assertEqual(result['candidate_files'], {})

    def test_rejected_unverified_inputs_do_not_echo_claimed_identity_digests(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        candidate = render('scoop', reg, snap)
        bad_reg, bad_snap = copy.deepcopy(reg), copy.deepcopy(snap)
        bad_reg['configuration_digest'] = 'f' * 64
        bad_snap['snapshot_digest'] = 'f' * 64
        for registration, snapshot in [(bad_reg, snap), (reg, bad_snap)]:
            result = self.assert_rejected('scoop', registration, snapshot, candidate)
            self.assertIsNone(result['configuration_digest'])
            self.assertIsNone(result['snapshot_digest'])
            with tempfile.TemporaryDirectory() as directory:
                result = validate_directory('scoop', registration, snapshot, directory)
            self.assertFalse(result['passed'])
            self.assertIsNone(result['configuration_digest'])
            self.assertIsNone(result['snapshot_digest'])

    def test_validation_evidence_explicitly_does_not_establish_release_authority(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        result = validate_candidate('scoop', reg, snap, render('scoop', reg, snap))
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result.get('authority'), 'not-evaluated')
        with tempfile.TemporaryDirectory() as directory:
            failed = validate_directory('scoop', reg, snap, Path(directory) / 'missing')
        self.assertFalse(failed['passed'])
        self.assertEqual(failed.get('authority'), 'not-evaluated')

    def test_winget_rejects_missing_or_mismatched_schema_headers(self):
        for app in ['patronus', 'lumen']:
            reg, snap = fixture(app, 'registration'), fixture(app, 'snapshot')
            candidate = render('winget', reg, snap)
            self.assertTrue(validate_candidate('winget', reg, snap, candidate)['passed'])
            for path, data in candidate.items():
                for changed in [data.split(b'\n', 1)[1],
                                data.replace(b'.1.10.0.schema.json', b'.1.9.0.schema.json', 1),
                                data.replace(b'winget-manifest.', b'other-manifest.', 1)]:
                    with self.subTest(app=app, path=path):
                        self.assert_rejected('winget', reg, snap,
                                             dict(candidate, **{path: changed}), 'canonical')

    def test_scoop_and_winget_require_the_canonical_rendered_bytes(self):
        reg, snap = fixture('patronus', 'registration'), fixture('patronus', 'snapshot')
        scoop = render('scoop', reg, snap)
        path = next(iter(scoop))
        doc = json.loads(scoop[path])
        variants = [b'\xef\xbb\xbf' + scoop[path],
                    scoop[path].decode().encode('utf-16'),
                    json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()]
        for index, data in enumerate(variants):
            with self.subTest(channel='scoop', variant=index):
                self.assert_rejected('scoop', reg, snap, {path: data}, 'canonical')
        winget = render('winget', reg, snap)
        flow = {path: ('# retained semantic data\n' + yaml.safe_dump(yaml.safe_load(data),
                    default_flow_style=True, sort_keys=True)).encode() for path, data in winget.items()}
        self.assert_rejected('winget', reg, snap, flow, 'canonical')
        lower = dict(winget)
        installer = next(path for path in lower if '.installer.' in path)
        doc = yaml.safe_load(lower[installer])
        for entry in doc['Installers']:
            entry['InstallerSha256'] = entry['InstallerSha256'].lower()
        lower[installer] = yaml.safe_dump(doc, sort_keys=False).encode()
        self.assert_rejected('winget', reg, snap, lower, 'canonical')


if __name__ == '__main__':
    unittest.main()
