"""Render and validate native candidates after T1 protected-source admission.

Neither structural input checks nor a validated package establish release authority.
The publisher must first use T1's load_authorized_snapshot and must bind these
results to the exact candidate bytes before writing. The module CLI is an offline
maintainer seam; T4 owns the distribution.cli wrapper and credentialed execution.
"""
import argparse
import hashlib
import json
from importlib import metadata
import os
from pathlib import Path
import platform
import stat
import sys
import subprocess
import tempfile

from .contracts import canonical_digest, fields, require, validate_registration
from .snapshot import strict_json, validate_snapshot

HERE = Path(__file__).resolve().parent
DRAFT = 'http://json-schema.org/draft-07/schema#'


class ValidationUnavailable(ValueError):
    """Pinned validation infrastructure is missing or has changed."""


def inputs(registration, snapshot):
    """Structural checks only; callers must first perform T1 protected admission."""
    registration = validate_registration(registration)
    snapshot = validate_snapshot(registration, snapshot)
    return registration['configuration'], snapshot['payload']['identity']


def asset_url(configuration, identity, asset):
    return (f"https://github.com/{configuration['repository']}/releases/download/"
            f"{identity['tag']}/{asset}")


def render(channel, registration, snapshot):
    """Return deterministic native path/byte pairs; never write or execute assets."""
    require(channel in {'brew', 'scoop', 'winget'}, 'unsupported channel')
    if channel == 'brew':
        from .brew import render as renderer
    elif channel == 'scoop':
        from .scoop import render as renderer
    else:
        from .winget import render as renderer
    return renderer(registration, snapshot)


def validate_candidate(channel, registration, snapshot, candidate):
    """Return byte-bound pass/fail evidence from actual parsers/schemas/native tools."""
    result = empty_result(channel)
    try:
        c, identity = bind_inputs(result, registration, snapshot)
        require(type(candidate) is dict and candidate and
                all(type(path) is str and type(data) is bytes for path, data in candidate.items()),
                'candidate must map paths to bytes')
        result['candidate_files'] = {p: hashlib.sha256(b).hexdigest() for p, b in sorted(candidate.items())}
        result['candidate_digest'] = canonical_digest(result['candidate_files'])
        require(channel in {'brew', 'scoop', 'winget'}, 'unsupported validation channel')
        paths = expected_paths(channel, c, identity)
        require(set(candidate) == set(paths), 'unexpected or incomplete candidate paths')
        require(all(0 < len(b) <= 1024 * 1024 for b in candidate.values()),
                'candidate size bound exceeded or empty file')
        schemas, registry, evidence = validation_environment()
        result['validation'] = evidence
        if channel == 'brew':
            require(candidate == render('brew', registration, snapshot),
                    'formula bytes differ from approved deterministic definition')
            native_brew(candidate, result['validation'])
        elif channel == 'scoop':
            document = strict_json(next(iter(candidate.values())))
            check_schema(schemas['scoop'], registry, document)
            check_scoop(c, identity, document)
        else:
            documents = {}
            for path, kind in paths.items():
                document = strict_yaml(candidate[path])
                check_schema(schemas['winget-' + kind], registry, document)
                documents[kind] = document
            check_winget(c, identity, documents)
        if channel != 'brew':
            require(candidate == render(channel, registration, snapshot),
                    'candidate bytes differ from canonical renderer output')
        result.update(passed=True, state='validated', failure_code=None)
    except Exception as error:
        result['failure_code'] = ('validation-unavailable' if isinstance(error,
            (ValidationUnavailable, ImportError, FileNotFoundError)) else 'validation-failed')
        result['errors'].append(str(error))
    return result


def empty_result(channel):
    return dict(schema_version=1, channel=channel, passed=False, state='failed',
                errors=[], failure_code='validation-failed', validation={}, authority='not-evaluated',
                candidate_files={}, candidate_digest=None,
                snapshot_digest=None, configuration_digest=None)


def bind_inputs(result, registration, snapshot):
    c, identity = inputs(registration, snapshot)
    result.update(app=c['app'], version=identity['version'],
                  snapshot_digest=snapshot['snapshot_digest'],
                  configuration_digest=registration['configuration_digest'])
    return c, identity


def native_brew(candidate, evidence):
    """Syntax/style only. Installation and brew test belong to isolated native checks."""
    native = dict(host=platform.system() + ' ' + platform.release(), machine=platform.machine(),
                  runner_image=os.environ.get('ImageOS'), runner_image_version=os.environ.get('ImageVersion'),
                  commands=[])
    evidence['native'] = native
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'TEMP', 'TMP',
               'TERM', 'LANG', 'LC_ALL', 'LC_CTYPE', 'CI', 'XDG_CONFIG_HOME',
               'HOMEBREW_CACHE', 'HOMEBREW_TEMP', 'HOMEBREW_LOGS', 'HOMEBREW_DEVELOPER',
               'HOMEBREW_BUNDLE_USER_CACHE', 'HOMEBREW_NO_INSTALL_FROM_API',
               'HOMEBREW_NO_INSTALL_CLEANUP'}
    env = {name: value for name, value in os.environ.items() if name in allowed}
    env.update(HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1')
    def command(args, *, candidate_check=False, timeout=60):
        try:
            process = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            native['commands'].append(dict(command=args, error=str(error)))
            raise ValidationUnavailable(str(error)) from error
        native['commands'].append(dict(command=args, exit=process.returncode,
            stdout=process.stdout[-8192:], stderr=process.stderr[-8192:]))
        if process.returncode != 0:
            error = 'native command failed: ' + repr(args) + ': ' + (process.stdout + process.stderr)[-8192:]
            raise (ValueError if candidate_check else ValidationUnavailable)(error)
        return process.stdout.strip()
    native['brew_version'] = command(['brew', '--version'])
    repo = command(['brew', '--repo'])
    native['brew_commit'] = command(['git', '-C', repo, 'rev-parse', 'HEAD'])
    with tempfile.TemporaryDirectory(prefix='patronus-validation-') as directory:
        bootstrap = Path(directory) / 'bootstrap.rb'
        bootstrap.write_text('# frozen_string_literal: true\n')
        command(['brew', 'style', '--only-cops', 'Lint/Syntax', str(bootstrap)], timeout=300)
        native['ruby_version'] = command(['brew', 'ruby', '--', '--version'])
        for path, data in candidate.items():
            target = Path(directory) / path
            target.parent.mkdir(parents=True)
            target.write_bytes(data)
            command(['brew', 'ruby', '--', '-c', str(target)], candidate_check=True)
            command(['brew', 'style', str(target)], candidate_check=True)


def expected_paths(channel, c, identity):
    if channel in {'brew', 'scoop'}:
        return {c['destinations'][channel]['path']: channel}
    require(channel == 'winget', 'unsupported channel')
    directory = c['destinations']['winget']['path'] + '/' + identity['version']
    name = directory + '/' + c['identifiers']['winget']
    return {name + suffix + '.yaml': kind for suffix, kind in
            [('', 'version'), ('.installer', 'installer'), ('.locale.en-US', 'defaultLocale')]}


def validation_environment():
    """Verify pinned inputs; schema resolution never invokes a network retriever."""
    try:
        from jsonschema import Draft7Validator
        from referencing import Registry, Resource
        from referencing.exceptions import NoSuchResource
        pins_bytes = (HERE / 'validator-pins.json').read_bytes()
        pins = strict_json(pins_bytes)
        require((3, 12) <= sys.version_info[:2] < (3, 15), 'Python 3.12-3.14 required')
        require(pins['json_schema_draft'] == DRAFT, 'unsupported JSON Schema draft')
        require(hashlib.sha256((HERE / 'validation-requirements.txt').read_bytes()).hexdigest()
                == pins['requirements_sha256'], 'dependency lock digest mismatch')
        versions = {}
        for name, pin in pins['dependencies'].items():
            if name == 'typing_extensions' and sys.version_info >= (3, 13):
                continue
            actual = metadata.version(name)
            require(actual == pin['version'], 'dependency version mismatch: ' + name)
            versions[name] = actual
        def deny_reference(uri):
            raise NoSuchResource(ref=uri)
        registry = Registry(retrieve=deny_reference)
        schemas = {}
        for name, pin in pins['schemas'].items():
            path = HERE / pin['path']
            require(path.parent == HERE / 'schemas' and not path.is_symlink(), 'unsafe schema path')
            raw = path.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == pin['sha256'], 'schema digest mismatch: ' + name)
            document = strict_json(raw)
            require(document['$schema'] == DRAFT == pin['draft']
                    and document['$id'] == pin['id'], 'schema identity/draft mismatch')
            Draft7Validator.check_schema(document)
            schemas[name] = document
            registry = registry.with_resource(document['$id'].rstrip('#'), Resource.from_contents(document))
        # Validate the complete reference closure, including unused definitions.
        def references(value):
            if type(value) is dict:
                for key, child in value.items():
                    if key == '$ref':
                        yield child
                    yield from references(child)
            elif type(value) is list:
                for child in value:
                    yield from references(child)
        for document in schemas.values():
            resolver = registry.resolver(document['$id'].rstrip('#'))
            for reference in references(document):
                resolver.lookup(reference)
        evidence = dict(pins_sha256=hashlib.sha256(pins_bytes).hexdigest(),
            requirements_sha256=pins['requirements_sha256'], schemas=pins['schemas'],
            dependencies=versions, python=sys.version, format_checking=True,
            reference_resolution='vendored-only')
        return schemas, registry, evidence
    except Exception as error:
        raise ValidationUnavailable(str(error)) from error


def check_schema(schema, registry, document):
    from jsonschema import Draft7Validator, FormatChecker
    validator = Draft7Validator(schema, registry=registry, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda e: str(list(e.path)))
    require(not errors, '; '.join(f'{list(e.path)}: {e.message}' for e in errors))


def strict_yaml(data):
    import yaml
    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                require(type(key) is str, 'YAML mapping keys must be strings')
                require(key not in result, 'duplicate YAML key: ' + key)
                result[key] = self.construct_object(value_node, deep=deep)
            return result
    for token in yaml.scan(data):
        require(not isinstance(token, yaml.tokens.TagToken), 'explicit YAML tag is forbidden')
        require(not isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)),
                'YAML aliases/anchors are forbidden')
    document = yaml.load(data, Loader=UniqueLoader)
    canonical_digest(document)  # Refuse timestamps, floats and other non-JSON types.
    return document


def check_scoop(c, identity, document):
    fields(document, 'version description homepage license architecture')
    require(document['version'] == identity['version'], 'Scoop version mismatch')
    for field in ['description', 'homepage', 'license']:
        require(document[field] == c[field], 'Scoop registered metadata mismatch: ' + field)
    fields(document['architecture'], '64bit arm64')
    for native, cpu in [('64bit', 'amd64'), ('arm64', 'arm64')]:
        entry = document['architecture'][native]
        fields(entry, 'url hash bin')
        asset = c['assets']['windows-' + cpu]
        require(entry['url'] == asset_url(c, identity, asset)
                and entry['hash'] == identity['assets'][asset]['sha256']
                and entry['bin'] == [[asset, c['identifiers']['command']]],
                'Scoop architecture, URL, hash or command mismatch: ' + native)


def check_winget(c, identity, documents):
    common = 'PackageIdentifier PackageVersion ManifestType ManifestVersion '
    for kind, document in documents.items():
        require(document['PackageIdentifier'] == c['identifiers']['winget']
                and document['PackageVersion'] == identity['version']
                and document['ManifestType'] == kind and document['ManifestVersion'] == '1.10.0',
                'WinGet cross-file identity, version or type mismatch: ' + kind)
    version, installer, locale = (documents[k] for k in ['version', 'installer', 'defaultLocale'])
    fields(version, common + 'DefaultLocale')
    require(version['DefaultLocale'] == 'en-US', 'WinGet default locale mismatch')
    fields(locale, common + 'PackageLocale Publisher PackageName PackageUrl License ShortDescription')
    require(locale['PackageLocale'] == 'en-US' and locale['Publisher'] == c['owner_login']
            and locale['PackageName'] == c['identifiers']['winget'].split('.')[1]
            and locale['PackageUrl'] == c['homepage'] and locale['License'] == c['license']
            and locale['ShortDescription'] == c['description'], 'WinGet registered locale metadata mismatch')
    fields(installer, common + 'InstallerType Scope Commands Installers')
    require(installer['InstallerType'] == 'portable' and installer['Scope'] == 'user'
            and installer['Commands'] == [c['identifiers']['command']], 'WinGet portable command/scope mismatch')
    entries = installer['Installers']
    require(type(entries) is list and len(entries) == 2, 'WinGet installer set mismatch')
    seen = set()
    for entry in entries:
        fields(entry, 'Architecture InstallerUrl InstallerSha256')
        native = entry['Architecture']
        require(native in {'x64', 'arm64'} and native not in seen, 'WinGet architecture mismatch')
        seen.add(native)
        asset = c['assets']['windows-' + {'x64': 'amd64', 'arm64': 'arm64'}[native]]
        require(entry['InstallerUrl'] == asset_url(c, identity, asset)
                and entry['InstallerSha256'].lower() == identity['assets'][asset]['sha256'],
                'WinGet frozen URL/hash mismatch: ' + native)


def read_regular(path):
    """Bounded local input read; reject symlinks and special files before reading."""
    require(not path.is_symlink(), 'symlink input is forbidden: ' + str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), 'input must be a regular file')
        require(0 < info.st_size <= 1024 * 1024, 'input size bound exceeded')
        data = stream.read(1024 * 1024 + 1)
        require(0 < len(data) <= 1024 * 1024, 'input size bound exceeded')
        return data


def validate_directory(channel, registration, snapshot, directory):
    """Maintainer/T4 seam: inspect a native candidate tree without following links."""
    result = empty_result(channel)
    try:
        c, identity = bind_inputs(result, registration, snapshot)
        paths = expected_paths(channel, c, identity)
        directory = Path(directory)
        require(not directory.is_symlink() and directory.is_dir(), 'candidate root must be a regular directory, not a symlink')
        allowed_dirs = {str(parent) for path in paths for parent in Path(path).parents if str(parent) != '.'}
        candidate = {}
        for current, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs:
                path = Path(current) / name
                require(not path.is_symlink(), 'symlink candidate directory forbidden')
                require(path.relative_to(directory).as_posix() in allowed_dirs, 'unexpected candidate directory path')
            for name in files:
                path = Path(current) / name
                relative = path.relative_to(directory).as_posix()
                require(not path.is_symlink(), 'symlink candidate file forbidden')
                require(relative in paths, 'unexpected candidate file path')
                candidate[relative] = read_regular(path)
        return validate_candidate(channel, registration, snapshot, candidate)
    except Exception as error:
        result['errors'] = [str(error)]
        return result


def main(argv=None):
    """T3 module CLI; T4 owns the distribution.cli validate wrapper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', required=True, choices=['brew', 'scoop', 'winget'])
    for name in ['app', 'snapshot', 'registration', 'candidate-dir', 'out']:
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    registration, snapshot = None, None
    result = empty_result(args.channel)
    try:
        registration = strict_json(read_regular(Path(args.registration)))
        snapshot = strict_json(read_regular(Path(args.snapshot)))
        c, _ = bind_inputs(result, registration, snapshot)
        require(args.app == c['app'], 'requested app differs from registration')
        result = validate_directory(args.channel, registration, snapshot, args.candidate_dir)
    except Exception as error:
        result['errors'] = [str(error)]
    Path(args.out).write_text(json.dumps(result, indent=2) + '\n')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
