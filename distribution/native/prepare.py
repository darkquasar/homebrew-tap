"""Native-check inputs for distribution/native and .github/workflows/native-checks.yml.

Canonical candidates are exactly the accepted renderer bytes. Only the separate
staging copies may carry loopback transport URLs, and the bottled formula is
staged only through bottles.staging_formula. Nothing here holds credentials,
publishes, or executes release contents.
"""
import hashlib
import json
from pathlib import Path
import re

from distribution.bottles import LOOPBACK
from distribution.contracts import require
from distribution.state import encode, file_digest
from distribution.validation import asset_url, inputs, render, validate_candidate

CHANNELS = ('brew', 'scoop', 'winget')
NEGATIVES = ('arch', 'version')


def _write(out, relative, data, files):
    target = Path(out) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    files[relative] = hashlib.sha256(data).hexdigest()


def loopback_root(url):
    """A plain loopback http root (no trailing slash), or ValueError."""
    require(type(url) is str and re.fullmatch(LOOPBACK, url) is not None,
            'staging URL must be a plain loopback http URL: ' + repr(url))
    return url.rstrip('/')


def _swap(text, first, second):
    marker = '\0swap\0'
    require(marker not in text, 'unexpected swap marker')
    return text.replace(first, marker).replace(second, first).replace(marker, second)


def staging_copy(channel, registration, snapshot, canonical, loopback, expect_failure=None):
    """Transport-only staging bytes; with expect_failure='arch' the two CPU mappings are swapped."""
    import yaml
    c, identity = inputs(registration, snapshot)
    if loopback is None:
        return dict(canonical)
    local = lambda url: loopback + '/' + url.rsplit('/', 1)[1]
    staged = {}
    for path, data in canonical.items():
        if channel == 'scoop':
            document = json.loads(data)
            architecture = document['architecture']
            if expect_failure == 'arch':
                architecture['64bit'], architecture['arm64'] = architecture['arm64'], architecture['64bit']
            for entry in architecture.values():
                entry['url'] = local(entry['url'])
            data = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode()
        elif channel == 'winget' and path.endswith('.installer.yaml'):
            header = data.splitlines(keepends=True)[0]
            require(header.startswith(b'# yaml-language-server: '), 'WinGet schema header missing')
            document = yaml.safe_load(data)
            entries = document['Installers']
            if expect_failure == 'arch':
                first, second = entries
                for field in ['InstallerUrl', 'InstallerSha256']:
                    first[field], second[field] = second[field], first[field]
            for entry in entries:
                entry['InstallerUrl'] = local(entry['InstallerUrl'])
            data = header + yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=1000).encode()
        elif channel == 'brew':
            text = data.decode()
            for platform, asset in c['assets'].items():
                if platform.startswith('windows-'):
                    continue
                line = f'url "{asset_url(c, identity, asset)}"'
                require(text.count(line) == 1, 'formula url line not found once: ' + asset)
                text = text.replace(line, f'url "{loopback}/{asset}"')
            if expect_failure == 'arch':
                for system in ['darwin', 'linux']:
                    amd, arm = c['assets'][system + '-amd64'], c['assets'][system + '-arm64']
                    text = _swap(text, amd, arm)
                    text = _swap(text, identity['assets'][amd]['sha256'], identity['assets'][arm]['sha256'])
            data = text.encode()
        staged[path] = data
    return staged


def fixture_snapshot(registration, version, assets):
    """Freeze synthetic release bytes as T1 does, with an explicit fixture-only baseline reference."""
    from distribution.contracts import OWNER_ID, canonical_digest, validate_registration
    from distribution.snapshot import freeze, validate_snapshot
    c = validate_registration(registration)['configuration']
    names = set(c['assets'].values())
    require(type(assets) is dict and set(assets) == names and all(type(v) is bytes and v for v in assets.values()),
            'fixture assets must be exactly the registered asset names')
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in assets.items()}
    checksums = ''.join(f'{digests[name]}  {name}\n' for name in sorted(assets)).encode()
    identity = dict(app=c['app'], repository=c['repository'], tag='v' + version, version=version,
                    commit='a' * 40, checksums_sha256=hashlib.sha256(checksums).hexdigest(),
                    assets={name: dict(size=len(assets[name]), sha256=digests[name]) for name in sorted(assets)})
    snapshot = freeze(registration, identity, dict(kind='baseline', owner_id=OWNER_ID,
                      reference='fixture-only:native-checks', identity_digest=canonical_digest(identity)))
    return validate_snapshot(registration, snapshot)


def native_names(registration, version):
    """Manager-facing names for one registered app: package identity and command stay separate."""
    from distribution.contracts import validate_registration
    c = validate_registration(registration)['configuration']
    owner, repository = c['destinations']['brew']['repository'].split('/')
    require(repository.startswith('homebrew-'), 'tap repository must be named homebrew-<tap>')
    return dict(app=c['app'], tap=owner + '/' + repository[len('homebrew-'):], formula=c['identifiers']['brew'],
                formula_path=c['destinations']['brew']['path'], scoop_bucket=c['owner_login'],
                scoop_package=c['identifiers']['scoop'], scoop_path=c['destinations']['scoop']['path'],
                winget_identifier=c['identifiers']['winget'],
                winget_path=c['destinations']['winget']['path'] + '/' + version,
                command=c['identifiers']['command'], assets=dict(c['assets']))


def version_matches(output, version):
    """`v<version>` as a whole token: v2.7.1 matches, v2.7.10, v2.7.1-rc1 and v2.7.0 do not."""
    return type(output) is str and re.search(
        r'(?<![0-9A-Za-z.+-])v' + re.escape(version) + r'(?![0-9A-Za-z.+-])', output) is not None


def check_record(registration, snapshot, record_ref):
    """The release-mode locator must name exactly this snapshot and configuration."""
    c, identity = inputs(registration, snapshot)
    require(type(record_ref) is dict and (record_ref.get('app'), record_ref.get('version'),
            record_ref.get('snapshot_digest'), record_ref.get('configuration_digest')) == (
            c['app'], identity['version'], snapshot['snapshot_digest'], registration['configuration_digest']),
            'record reference does not locate this snapshot and configuration')


def stage_candidates(out, registration, snapshot, *, loopback=None, assets=None, expect_failure=None,
                     record_ref=None):
    """Write canonical and staging candidates plus prepare.json; return that result.

    Release mode passes record_ref and no loopback: staging equals canonical and
    managers download the real release URLs. Fixture mode passes the synthetic
    assets (checked against the snapshot) and the loopback root that serves them.
    """
    c, identity = inputs(registration, snapshot)
    if record_ref is not None:
        check_record(registration, snapshot, record_ref)
    require((loopback is None) == (assets is None), 'fixture assets and a loopback URL go together')
    if loopback is not None:
        loopback = loopback_root(loopback)
        require(type(assets) is dict and set(assets) == set(identity['assets']),
                'fixture assets missing or extra: ' + ', '.join(sorted(set(identity['assets']) ^ set(assets or {}))))
        for name in sorted(assets):
            require(type(assets[name]) is bytes
                    and hashlib.sha256(assets[name]).hexdigest() == identity['assets'][name]['sha256'],
                    'fixture asset differs from the snapshot: ' + name)
    require(expect_failure is None or (expect_failure in NEGATIVES and loopback is not None),
            'expect_failure must be arch or version, in fixture mode only')
    out = Path(out)
    out.mkdir(parents=True)
    files, candidates, validation = {}, {}, {}
    _write(out, 'registration.json', encode(registration), files)
    _write(out, 'snapshot.json', encode(snapshot), files)
    for name in sorted(assets or {}):
        _write(out, 'assets/' + name, assets[name], files)
    for channel in CHANNELS:
        canonical = render(channel, registration, snapshot)
        candidates[channel] = file_digest(canonical)
        if channel != 'brew':
            # Brew syntax/style needs Homebrew; the brew-check job validates the bottled formula natively.
            validation[channel] = validate_candidate(channel, registration, snapshot, canonical)
            require(validation[channel]['passed'], f'{channel} candidate failed validation: '
                    + '; '.join(validation[channel]['errors']))
        staged = staging_copy(channel, registration, snapshot, canonical, loopback, expect_failure)
        for path, data in canonical.items():
            _write(out, f'canonical/{channel}/{path}', data, files)
            _write(out, f'staging/{channel}/{path}', staged[path], files)
    result = dict(schema_version=1, app=c['app'], version=identity['version'],
                  snapshot_digest=snapshot['snapshot_digest'],
                  configuration_digest=registration['configuration_digest'],
                  loopback=loopback, expect_failure=expect_failure, candidates=candidates,
                  validation=validation, names=native_names(registration, identity['version']), files=files)
    (out / 'prepare.json').write_bytes(encode(result))
    return result


def stage_bottles(out, registration, snapshot, archives, *, source, loopback):
    """Verify four archives, assemble the bottle_set and stage the bottled formula.

    Writes bottles/<remote filename>, canonical/brew (the publishable bottled
    formula), staging/brew (bottles.staging_formula with the loopback root that
    serves those archives) and brew.json with bottled_formula_identity.
    """
    from distribution.bottles import (bottle_set, bottled_formula_identity, canonical_root_url,
                                      published_tag, staging_formula, verify_bottle)
    c, identity = inputs(registration, snapshot)
    require(source in {'fresh', 'published'}, 'bottle source must be fresh or published')
    loopback = loopback_root(loopback)
    require(type(archives) is dict, 'archives must map remote filenames to bytes')
    verified = {}
    for name in sorted(archives):
        verified[name] = verify_bottle(archives[name], registration, snapshot,
                                       published_tag(registration, snapshot, name))
    bottles = bottle_set(registration, snapshot, list(verified.values()), canonical_root_url(c, identity))
    canonical = render('brew', registration, snapshot, bottles)
    out = Path(out)
    out.mkdir(parents=True)
    files = {}
    for name in sorted(archives):
        _write(out, 'bottles/' + name, archives[name], files)
    for path, data in canonical.items():
        _write(out, 'canonical/brew/' + path, data, files)
        _write(out, 'staging/brew/' + path, staging_formula(data, loopback), files)
    result = dict(schema_version=1, app=c['app'], version=identity['version'],
                  snapshot_digest=snapshot['snapshot_digest'],
                  configuration_digest=registration['configuration_digest'], source=source,
                  loopback=loopback, bottle_set=bottles,
                  identity=bottled_formula_identity(registration, snapshot, bottles),
                  verified=verified, files=files)
    (out / 'brew.json').write_bytes(encode(result))
    return result


# Platform reports written by brew_check.sh and windows_check.ps1 (one per manager and platform).
REPORT_FIELDS = {'schema_version', 'stage', 'manager', 'platform', 'outcome', 'reason', 'runner', 'image',
                 'image_version', 'manager_version', 'steps', 'inputs_sha256'}
NOT_RUN = dict(validate='not-run', install='not-run', version='not-run', uninstall='not-run')


def _report_key(document):
    """(stage, manager, platform) for a structurally valid report, else None."""
    from distribution.bottles import MANAGER_PLATFORMS, STEPS, STEP_STATES
    from distribution.contracts import DIGEST, matches
    try:
        if not (type(document) is dict and set(document) == REPORT_FIELDS and document['schema_version'] == 1
                and document['stage'] in {'build', 'check'} and document['manager'] in MANAGER_PLATFORMS
                and document['platform'] in MANAGER_PLATFORMS[document['manager']]
                and (document['stage'] == 'check' or document['manager'] == 'brew')
                and document['outcome'] in {'passed', 'failed'} and matches(DIGEST, document['inputs_sha256'])
                and type(document['steps']) is dict and set(document['steps']) == set(STEPS)
                and all(document['steps'][step] in STEP_STATES for step in STEPS)
                and all(type(document[name]) is str and len(document[name]) <= 1024
                        and not any(ord(ch) < 32 for ch in document[name])
                        for name in ['reason', 'runner', 'image', 'image_version', 'manager_version'])):
            return None
        if document['outcome'] == 'passed' and (document['stage'] != 'check'
                                                or set(document['steps'].values()) != {'passed'}):
            return None
        return document['stage'], document['manager'], document['platform']
    except (KeyError, TypeError):
        return None


def summarize(prepared, brew, reports, *, run_id, attempt, static=None, brew_failure=None):
    """native-checks.json schema v1 from this run's inputs and platform reports.

    prepared and brew are the exact prepare.json and brew.json bytes the platform
    jobs consumed; a report counts only when its inputs_sha256 names those bytes.
    A missing, malformed or duplicated report is a failure for its platform. A
    platform in `static` without a report is recorded as statically mapped.
    """
    from distribution.bottles import MANAGER_PLATFORMS, validate_native_checks
    from distribution.contracts import CONTROL_REPOSITORY
    from distribution.snapshot import strict_json
    plan = strict_json(prepared)
    digests = dict(prepare=hashlib.sha256(prepared).hexdigest())
    bottled = None
    if brew is not None:
        bottled = strict_json(brew)
        require(all(bottled.get(k) == plan[k] for k in ['app', 'version', 'snapshot_digest', 'configuration_digest']),
                'brew.json belongs to a different app, version, snapshot or configuration')
        digests['brew'] = hashlib.sha256(brew).hexdigest()
    static = static or {}
    found, duplicates = {}, set()
    for document in reports:
        key = _report_key(document)
        if key is None:
            continue
        if key in found:
            duplicates.add(key)
        found[key] = document
    results = []
    for manager, platforms in MANAGER_PLATFORMS.items():
        if manager == 'brew':
            binding = bottled['identity']['formula_sha256'] if bottled else plan['candidates']['brew']
        else:
            binding = plan['candidates'][manager]
        for platform in platforms:
            entry = None
            for stage, inputs_digest in [('check', digests.get('brew' if manager == 'brew' else 'prepare')),
                                         ('build', digests['prepare'])]:
                key = (stage, manager, platform)
                if key in duplicates:
                    entry = dict(outcome='failed', reason='duplicate results', steps=dict(NOT_RUN))
                elif key in found and (stage == 'check' or found[key]['outcome'] == 'failed'):
                    document = found[key]
                    entry = {name: document[name] for name in
                             ['outcome', 'reason', 'runner', 'image', 'image_version', 'manager_version', 'steps']}
                    if document['inputs_sha256'] != inputs_digest:
                        entry.update(outcome='failed', reason='checked different inputs: ' + document['reason'])
                if entry is not None:
                    break
            if entry is None and platform in static:
                entry = dict(outcome='statically-mapped', reason=static[platform], steps=dict(NOT_RUN))
            if entry is None:
                reason = brew_failure if (manager == 'brew' and bottled is None and brew_failure) else 'no result'
                entry = dict(outcome='failed', reason=reason, steps=dict(NOT_RUN))
            base = dict(runner='', image='', image_version='', manager_version='')
            results.append(dict(base, **entry, manager=manager, platform=platform, binding=binding))
    document = dict(schema_version=1, app=plan['app'], version=plan['version'],
                    snapshot_digest=plan['snapshot_digest'], configuration_digest=plan['configuration_digest'],
                    candidates={k: plan['candidates'][k] for k in ['scoop', 'winget']},
                    brew=None if bottled is None else {k: bottled[k] for k in ['source', 'bottle_set', 'identity']},
                    results=results,
                    run=dict(id=run_id, attempt=attempt,
                             url=f'https://github.com/{CONTROL_REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}'))
    return validate_native_checks(document)


DIAGNOSTICS = dict(arch='architecture-mismatch:', version='version-mismatch:')


def expected_failure(document, kind, registration, snapshot):
    """Problems with an AC-17 negative run; [] means every exercised check failed as intended.

    Each non-static result must fail with the matching diagnostic, and the
    publisher's gate (bottles.native_failures) must stop every channel.
    """
    from distribution.bottles import native_failures, validate_native_checks
    require(kind in DIAGNOSTICS, 'expect_failure must be arch or version')
    document = validate_native_checks(document)
    problems = []
    for entry in document['results']:
        if entry['outcome'] == 'statically-mapped':
            continue
        if entry['outcome'] != 'failed' or not entry['reason'].startswith(DIAGNOSTICS[kind]):
            problems.append(f"{entry['manager']}/{entry['platform']}: {entry['outcome']} "
                            f"({entry['reason'] or 'no diagnostic'}), expected {DIAGNOSTICS[kind]}")
    brew = document['brew']
    for channel in ['brew', 'scoop', 'winget']:
        if channel == 'brew':
            binding = brew['identity']['formula_sha256'] if brew else None
            bottles = brew['bottle_set'] if brew else None
        else:
            binding, bottles = document['candidates'][channel], None
        if not native_failures(channel, registration, snapshot, document, binding, bottles=bottles):
            problems.append(channel + ': the publisher gate would accept this evidence')
    return problems


def _preservation_targets(source, project, home):
    from distribution.snapshot import strict_json
    manifest = strict_json((Path(source) / 'manifest.json').read_bytes())['files']
    roots = dict(project=Path(project), home=Path(home))
    targets = {}
    for key in manifest:
        scope, relative = key.split('/', 1)
        require(scope in roots and all(part not in {'', '.', '..'} for part in relative.split('/')),
                'unsafe preservation manifest path: ' + key)
        targets[key] = roots[scope] / relative
    return manifest, roots, targets


def seed_preservation(source, project, home):
    """Copy the T5 sentinels into disposable roots, never over an existing file; return their hashes."""
    import shutil
    manifest, _roots, targets = _preservation_targets(source, project, home)
    for key, target in targets.items():
        require(not target.exists() and not target.is_symlink(), 'preservation target already exists: ' + str(target))
    for key, target in sorted(targets.items()):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source) / key, target)
    seeded = preservation_inventory(source, project, home)
    require(seeded == manifest, 'seeded sentinels differ from manifest.json')
    return seeded


def preservation_inventory(source, project, home):
    """Hashes of every sentinel (None when missing) plus any other file inside the seeded roots.

    The whole project is seeded content; in the home, each top-level seeded
    directory (under .config, its child) is compared completely, so additions
    count while unrelated manager-owned home files do not.
    """
    _manifest, roots, targets = _preservation_targets(source, project, home)
    inventory = {key: (hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                 for key, path in targets.items()}
    scanned = {roots['project']}
    for key in targets:
        scope, relative = key.split('/', 1)
        if scope == 'home':
            parts = relative.split('/')
            depth = 2 if parts[0] == '.config' and len(parts) > 2 else 1
            scanned.add(roots['home'].joinpath(*parts[:depth]))
    for root in scanned:
        if not root.is_dir():
            continue
        scope = 'project' if root == roots['project'] else 'home'
        for path in sorted(root.rglob('*')):
            if path.is_file() or path.is_symlink():
                key = scope + '/' + path.relative_to(roots[scope]).as_posix()
                if key not in inventory:
                    inventory[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'symlink'
    return inventory


def _json(path):
    from distribution.snapshot import strict_json
    from distribution.validation import read_regular
    return strict_json(read_regular(Path(path)))


def _select(prepare, out):
    """Release mode: unprivileged read of the published tap release (bottles.select_bottles)."""
    from distribution.bottles import BottleConflict, select_bottles
    from distribution.github import GitHub
    registration, snapshot = _json(prepare / 'registration.json'), _json(prepare / 'snapshot.json')
    out.mkdir(parents=True)
    try:
        selected = select_bottles(registration, snapshot, GitHub())
    except BottleConflict as error:
        selection = dict(source='conflict', reason='bottle-conflict: ' + str(error))
    except Exception as error:
        selection = dict(source='unavailable', reason='bottle selection unavailable: ' + str(error))
    else:
        if selected is None:
            selection = dict(source='fresh', reason='no published tap release; build fresh bottles')
        else:
            selection = dict(source='published', reason='published tap release selected')
            for name, data in selected['archives'].items():
                _write(out, 'archives/' + name, data, {})
    (out / 'selection.json').write_bytes(encode(selection))
    print(encode(selection).decode())
    return 0


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('fixture', help='freeze synthetic binaries and stage loopback candidates')
    command.add_argument('--app', default='patronus')
    command.add_argument('--binaries', required=True, help='directory with <version>/<asset> files')
    command.add_argument('--version', required=True)
    command.add_argument('--assets-version', help='take asset bytes from this version (version negative)')
    command.add_argument('--loopback', required=True)
    command.add_argument('--expect-failure', choices=NEGATIVES)
    command.add_argument('--out', required=True)
    command = sub.add_parser('release', help='stage the recorded snapshot with real release URLs')
    for name in ['registration', 'snapshot', 'record-ref', 'out']:
        command.add_argument('--' + name, required=True)
    command = sub.add_parser('select', help='select a published bottle set, read-only')
    command.add_argument('--prepare', required=True)
    command.add_argument('--out', required=True)
    command = sub.add_parser('bottles', help='verify archives and stage the bottled formula')
    for name in ['prepare', 'archives', 'loopback', 'out']:
        command.add_argument('--' + name, required=True)
    command.add_argument('--source', required=True, choices=['fresh', 'published'])
    command = sub.add_parser('summary', help='write native-checks.json')
    for name in ['prepare', 'brew', 'reports', 'pins', 'out']:
        command.add_argument('--' + name, required=True)
    command.add_argument('--run-id', required=True, type=int)
    command.add_argument('--attempt', required=True, type=int)
    command.add_argument('--brew-failure')
    command.add_argument('--selection', help='selection.json from the select job; missing means Brew failed')
    command.add_argument('--expect-failure', choices=NEGATIVES)
    command = sub.add_parser('validate-bottled', help='native syntax/style check of the canonical bottled formula')
    for name in ['prepare', 'brew', 'out']:
        command.add_argument('--' + name, required=True)
    command = sub.add_parser('report', help='write one platform report for the summary job')
    for name in ['stage', 'manager', 'platform', 'outcome', 'runner', 'manager-version', 'steps', 'inputs', 'out']:
        command.add_argument('--' + name, required=True)
    command.add_argument('--reason', default='')
    command = sub.add_parser('version-check', help='exact v<version> token in a command output')
    command.add_argument('--version', required=True)
    command.add_argument('--output', required=True)
    command = sub.add_parser('preservation', help='seed or compare the T5 preservation sentinels')
    command.add_argument('action', choices=['seed', 'check'])
    for name in ['source', 'project', 'home', 'out']:
        command.add_argument('--' + name, required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == 'fixture':
        registration = _json(Path(__file__).resolve().parent.parent / 'tests' / 'fixtures'
                             / f'{args.app}-native-registration.json')
        names = registration['configuration']['assets'].values()
        source = Path(args.binaries) / (args.assets_version or args.version)
        assets = {name: (source / name).read_bytes() for name in names}
        snapshot = fixture_snapshot(registration, args.version, assets)
        result = stage_candidates(Path(args.out), registration, snapshot, loopback=args.loopback,
                                  assets=assets, expect_failure=args.expect_failure)
    elif args.command == 'release':
        from distribution.contracts import validate_registration
        from distribution.snapshot import strict_json
        registration = validate_registration(_json(args.registration))
        result = stage_candidates(Path(args.out), registration, _json(args.snapshot),
                                  record_ref=strict_json(args.record_ref))
    elif args.command == 'validate-bottled':
        from distribution.bottles import staging_formula
        prepare, brew = Path(args.prepare), Path(args.brew)
        registration, snapshot, staged = _json(prepare / 'registration.json'), _json(prepare / 'snapshot.json'), _json(brew / 'brew.json')
        path = registration['configuration']['destinations']['brew']['path']
        canonical = {path: (brew / 'canonical' / 'brew' / path).read_bytes()}
        result = validate_candidate('brew', registration, snapshot, canonical, staged['bottle_set'])
        if (brew / 'staging' / 'brew' / path).read_bytes() != staging_formula(canonical[path], staged['loopback']):
            result.update(passed=False, state='failed', failure_code='validation-failed')
            result['errors'].append('staging formula is not staging_formula(canonical, loopback)')
        Path(args.out).write_bytes(encode(result))
        print(encode(result).decode())
        return 0 if result['passed'] else 1
    elif args.command == 'report':
        import os
        steps = dict(item.split('=', 1) for item in args.steps.split(','))
        document = dict(schema_version=1, stage=args.stage, manager=args.manager, platform=args.platform,
                        outcome=args.outcome, reason=args.reason[-1000:], runner=args.runner,
                        image=os.environ.get('ImageOS', ''), image_version=os.environ.get('ImageVersion', ''),
                        manager_version=args.manager_version[:1000], steps=steps,
                        inputs_sha256=hashlib.sha256(Path(args.inputs).read_bytes()).hexdigest())
        require(_report_key(document) is not None, 'invalid platform report: ' + repr(document))
        Path(args.out).write_bytes(encode(document))
        print(encode(document).decode())
        return 0
    elif args.command == 'version-check':
        if version_matches(args.output, args.version):
            print(f'version-ok: v{args.version}')
            return 0
        print(f'version-mismatch: expected v{args.version}, got: {args.output.strip()}')
        return 1
    elif args.command == 'preservation':
        if args.action == 'seed':
            inventory = seed_preservation(args.source, args.project, args.home)
        else:
            inventory = preservation_inventory(args.source, args.project, args.home)
        manifest = _preservation_targets(args.source, args.project, args.home)[0]
        differences = sorted(k for k in set(inventory) | set(manifest) if inventory.get(k) != manifest.get(k))
        result = dict(preserved=not differences, differences=differences, inventory=inventory)
        Path(args.out).write_bytes(encode(result))
        print(encode(result).decode())
        return 0 if not differences else 1
    elif args.command == 'select':
        return _select(Path(args.prepare), Path(args.out))
    elif args.command == 'bottles':
        prepare = Path(args.prepare)
        registration, snapshot = _json(prepare / 'registration.json'), _json(prepare / 'snapshot.json')
        # Lifecycle runs carry two versions' archives; take only this snapshot's version.
        prefix = f"{registration['configuration']['identifiers']['brew']}-{snapshot['payload']['identity']['version']}."
        archives = {}
        for path in sorted(Path(args.archives).rglob(prefix + '*.bottle.tar.gz')):
            require(path.is_file() and not path.is_symlink() and path.name not in archives,
                    'bottle archives must be distinct regular files: ' + path.name)
            archives[path.name] = path.read_bytes()
        result = stage_bottles(Path(args.out), registration, snapshot, archives, source=args.source,
                               loopback=args.loopback)
        result = {k: result[k] for k in ['source', 'bottle_set', 'identity', 'verified']}
    else:
        from distribution.validation import read_regular
        prepare = Path(args.prepare)
        reports = []
        for path in sorted(Path(args.reports).rglob('report-*.json')):
            try:
                reports.append(_json(path))
            except Exception as error:
                print(f'ignored unreadable report {path}: {error}')
        static = _json(args.pins)['statically_mapped']
        brew, failure, usable = None, args.brew_failure, True
        if args.selection is not None:
            # A missing or unreadable selection fails Brew only; Windows reports still count.
            try:
                selection = _json(args.selection)
                require(selection.get('source') in {'fresh', 'published', 'conflict', 'unavailable'},
                        'unknown selection source')
            except Exception as error:
                failure, usable = failure or f'bottle-selection-unavailable: no readable selection.json ({error})', False
            else:
                if selection['source'] in {'conflict', 'unavailable'}:
                    failure, usable = failure or selection['reason'], False
        if usable:
            try:
                brew = read_regular(Path(args.brew)) if args.brew else None
            except Exception as error:
                brew = None
                failure = failure or f'bottle-assembly-failed: no complete verified bottle set ({error})'
            if brew is None:
                failure = failure or 'bottle-assembly-failed: no complete verified bottle set'
        document = summarize(read_regular(prepare), brew, reports, run_id=args.run_id, attempt=args.attempt,
                             static=static, brew_failure=failure)
        Path(args.out).write_bytes(encode(document))
        print(encode(document).decode())
        if args.expect_failure:
            problems = expected_failure(document, args.expect_failure, _json(prepare.parent / 'registration.json'),
                                        _json(prepare.parent / 'snapshot.json'))
            for problem in problems:
                print('negative check not satisfied: ' + problem)
            return 1 if problems else 0
        return 0
    print(encode(result).decode())
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
