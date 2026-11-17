"""Homebrew bottle verification. Never executes or extracts archive contents."""
import copy
import hashlib
import io
import re
import tarfile
from urllib.parse import quote
import zlib

from .contracts import DIGEST, canonical_digest, matches
from .validation import inputs

MAX_UNCOMPRESSED = 256 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
# Homebrew 7.0.10 (brew 8f10429b) Library/Homebrew/macos_version.rb MacOSVersion::SYMBOLS.
MACOS_RELEASES = {'golden_gate': '27', 'tahoe': '26', 'sequoia': '15', 'sonoma': '14',
                  'ventura': '13', 'monterey': '12', 'big_sur': '11'}
MACOS_TAGS = tuple(MACOS_RELEASES)
CELLAR = 'any_skip_relocation'
BREW_PLATFORMS = ('darwin-arm64', 'darwin-amd64', 'linux-amd64', 'linux-arm64')


class BottleInvalid(ValueError):
    """Structure: members, links, size or tag."""


class BottleConflict(ValueError):
    """Integrity: the bottled executable differs from the frozen snapshot asset."""


def bottle_tag_platform(tag):
    """Map one Homebrew bottle tag to (system, cpu); rebuild suffixes are not tags."""
    if type(tag) is str:
        if tag == 'x86_64_linux':
            return ('linux', 'amd64')
        if tag == 'arm64_linux':
            return ('linux', 'arm64')
        if tag in MACOS_TAGS:
            return ('darwin', 'amd64')
        if tag.startswith('arm64_') and tag[6:] in MACOS_TAGS:
            return ('darwin', 'arm64')
    raise BottleInvalid('unsupported bottle tag: ' + repr(tag))


def remote_filename(formula, version, tag):
    """Homebrew Bottle::Filename#url_encode for rebuild 0 (bottle.rb:52)."""
    return quote(f'{formula}-{version}.{tag}.bottle.tar.gz', safe='')


class _Inflater(io.RawIOBase):
    """Single-member gzip stream with a hard uncompressed bound and no trailing bytes."""
    def __init__(self, data):
        self.source = memoryview(data)
        self.offset = 0
        self.inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.pending = b''
        self.total = 0

    def readable(self):
        return True

    def readinto(self, target):
        while not self.pending and not self.inflater.eof:
            data = self.inflater.unconsumed_tail
            if not data:
                data = bytes(self.source[self.offset:self.offset + 65536])
                self.offset += len(data)
                if not data:
                    raise BottleInvalid('truncated gzip stream')
            try:
                self.pending = self.inflater.decompress(data, 65536)
            except zlib.error as error:
                raise BottleInvalid('invalid gzip stream: ' + str(error)) from None
            self.total += len(self.pending)
            if self.total > MAX_UNCOMPRESSED:
                raise BottleInvalid(f'uncompressed size exceeds {MAX_UNCOMPRESSED} bytes')
        if self.inflater.eof and (self.inflater.unused_data or self.offset < len(self.source)):
            raise BottleInvalid('trailing bytes after gzip stream')
        count = min(len(target), len(self.pending))
        target[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


def verify_bottle(archive, registration, snapshot, tag):
    """Structural and identity check of one bottle archive; nothing is written or run."""
    c, identity = inputs(registration, snapshot)
    system, cpu = bottle_tag_platform(tag)
    if type(archive) is not bytes or not 0 < len(archive) <= MAX_ARCHIVE:
        raise BottleInvalid('bottle archive must be 1 byte to 256 MiB of bytes')
    formula, version = c['identifiers']['brew'], identity['version']
    root = f'{formula}/{version}'
    executable = f"{root}/bin/{c['identifiers']['command']}"
    files = {executable, f'{root}/INSTALL_RECEIPT.json', f'{root}/.brew/{formula}.rb',
             f'{root}/sbom.spdx.json'}
    directories = {formula, root, f'{root}/bin', f'{root}/.brew'}
    seen = set()
    digest = None
    stream = _Inflater(archive)
    try:
        with tarfile.open(fileobj=io.BufferedReader(stream), mode='r|') as tar:
            for member in tar:
                name = member.name
                parts = name.split('/')
                if (not name or name.startswith('/') or '\\' in name or '\0' in name
                        or any(part in {'', '.', '..'} for part in parts)):
                    raise BottleInvalid('unsafe member path: ' + repr(name))
                if name in seen:
                    raise BottleInvalid('duplicate member: ' + name)
                seen.add(name)
                if member.issym() or member.islnk():
                    raise BottleInvalid('link member forbidden: ' + name)
                if member.mode & 0o7000:
                    raise BottleInvalid('special permission bits forbidden: ' + name)
                if member.isdir():
                    if name not in directories:
                        raise BottleInvalid('unexpected directory member: ' + name)
                elif member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    if name not in files:
                        raise BottleInvalid('unexpected file member: ' + name)
                    data = tar.extractfile(member).read()
                    if name == executable:
                        digest = hashlib.sha256(data).hexdigest()
                else:
                    raise BottleInvalid('special member forbidden: ' + name)
            while stream.readinto(bytearray(65536)):
                pass
    except tarfile.TarError as error:
        raise BottleInvalid('invalid tar stream: ' + str(error)) from None
    if digest is None:
        raise BottleInvalid('missing executable member: ' + executable)
    platform = system + '-' + cpu
    expected = identity['assets'][c['assets'][platform]]['sha256']
    if digest != expected:
        for other in BREW_PLATFORMS:
            if identity['assets'][c['assets'][other]]['sha256'] == digest:
                raise BottleInvalid(f'executable {executable} is the {other} asset, not {platform} for tag {tag}')
        raise BottleConflict(f'bottled executable {executable} sha256 {digest} differs from '
                             f'snapshot {platform} sha256 {expected}')
    return dict(tag=tag, sha256=hashlib.sha256(archive).hexdigest(), executable_sha256=digest,
                asset=remote_filename(formula, version, tag), members=sorted(seen))


def release_tag(configuration, identity):
    """The only tap release tag an app may use: <registered app>-<version>."""
    return f"{configuration['app']}-{identity['version']}"


def canonical_root_url(configuration, identity):
    return (f"https://github.com/{configuration['destinations']['brew']['repository']}"
            f"/releases/download/{release_tag(configuration, identity)}")


def check_bottle_set(registration, snapshot, bottles):
    """Structural bottle_set check bound to the registered app and frozen version."""
    c, identity = inputs(registration, snapshot)
    if type(bottles) is not dict or set(bottles) != {'release_tag', 'root_url', 'cellar', 'tags'}:
        raise BottleInvalid('bottle_set fields must be release_tag, root_url, cellar and tags')
    if bottles['release_tag'] != release_tag(c, identity):
        raise BottleInvalid(f"release tag {bottles['release_tag']!r} is not {release_tag(c, identity)}")
    if bottles['root_url'] != canonical_root_url(c, identity):
        raise BottleInvalid(f"root_url {bottles['root_url']!r} is not the canonical tap release URL")
    if bottles['cellar'] != CELLAR:
        raise BottleInvalid('only cellar :any_skip_relocation is accepted')
    tags = bottles['tags']
    if type(tags) is not dict:
        raise BottleInvalid('bottle tags must map tag to sha256')
    platforms = []
    for tag, digest in tags.items():
        platforms.append('-'.join(bottle_tag_platform(tag)))
        if not matches(DIGEST, digest):
            raise BottleInvalid('invalid bottle sha256 for tag ' + tag)
    if sorted(platforms) != sorted(BREW_PLATFORMS):
        raise BottleInvalid('bottle_set needs exactly one tag per Brew platform; got ' + ', '.join(sorted(tags)))
    return copy.deepcopy(bottles)


def bottle_set(registration, snapshot, verified, root_url):
    """Assemble verify_bottle results into one complete, canonical set."""
    c, identity = inputs(registration, snapshot)
    if type(verified) is not list:
        raise BottleInvalid('verified bottles must be a list')
    tags = {}
    for result in verified:
        if type(result) is not dict or set(result) != {'tag', 'sha256', 'executable_sha256', 'asset', 'members'}:
            raise BottleInvalid('verified bottle result has missing or unknown fields')
        tag = result['tag']
        platform = '-'.join(bottle_tag_platform(tag))
        expected = identity['assets'][c['assets'][platform]]['sha256']
        if result['executable_sha256'] != expected:
            raise BottleConflict(f'{tag} executable sha256 differs from snapshot {platform} {expected}')
        if result['asset'] != remote_filename(c['identifiers']['brew'], identity['version'], tag):
            raise BottleInvalid(f"bottle asset {result['asset']!r} is not the remote filename for {tag}")
        if tag in tags:
            raise BottleInvalid('duplicate bottle tag ' + tag)
        tags[tag] = result['sha256']
    return check_bottle_set(registration, snapshot, dict(release_tag=release_tag(c, identity),
                            root_url=root_url, cellar=CELLAR, tags=tags))


def ordered_tags(tags):
    """Homebrew BottleSpecification#checksums order, which `brew bottle --merge` writes."""
    def key(tag):
        system, cpu = bottle_tag_platform(tag)
        if system == 'darwin':
            name = tag[6:] if cpu == 'arm64' else tag
            return f"{3 if cpu == 'arm64' else 2}.{MACOS_RELEASES[name]}_{tag}"
        return f"{1 if cpu == 'arm64' else 0}.{tag}"
    return sorted(tags, key=key, reverse=True)


def bottle_block(registration, snapshot, bottles):
    """Formula lines for a checked bottle_set, aligned as Homebrew's bottle_output aligns them."""
    from .brew import ruby_string
    bottles = check_bottle_set(registration, snapshot, bottles)
    tags = ordered_tags(bottles['tags'])
    prefix = f'sha256 cellar: :{CELLAR}, '
    width = max(len(tag) for tag in tags) + 2
    lines = ['  bottle do', f"    root_url {ruby_string(bottles['root_url'])}"]
    for tag in tags:
        lines.append(f"    {prefix}{(tag + ':').ljust(width)}{ruby_string(bottles['tags'][tag])}")
    lines.append('  end')
    return '\n'.join(lines) + '\n'


def bottled_formula_identity(registration, snapshot, bottles):
    """The one identity that native checks bind to and the publisher reproduces."""
    from .validation import render
    c, _identity = inputs(registration, snapshot)
    checked = check_bottle_set(registration, snapshot, bottles)
    formula = render('brew', registration, snapshot, checked)[c['destinations']['brew']['path']]
    return dict(formula_sha256=hashlib.sha256(formula).hexdigest(), bottle_set_sha256=canonical_digest(checked),
                snapshot_digest=snapshot['snapshot_digest'], configuration_digest=registration['configuration_digest'])


LOOPBACK = r'http://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*/?'
CANONICAL_ROOT_LINE = (r'    root_url "https://github\.com/darkquasar/homebrew-tap/releases/download/'
                       r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"')


def staging_formula(canonical, loopback_root_url):
    """Replace only the canonical root_url line with a loopback URL; all other bytes stay identical."""
    if type(canonical) is not bytes or type(loopback_root_url) is not str:
        raise ValueError('canonical formula bytes and a loopback URL string are required')
    if not re.fullmatch(LOOPBACK, loopback_root_url):
        raise ValueError('staging root_url must be a plain loopback http URL')
    lines = canonical.decode('utf-8').split('\n')
    found = [i for i, line in enumerate(lines) if 'root_url' in line]
    if (len(found) != 1 or found[0] == 0 or lines[found[0] - 1] != '  bottle do'
            or not re.fullmatch(CANONICAL_ROOT_LINE, lines[found[0]])):
        raise ValueError('canonical formula must have exactly one bottle root_url line')
    staged = list(lines)
    staged[found[0]] = f'    root_url "{loopback_root_url}"'
    changed = [i for i, (a, b) in enumerate(zip(lines, staged)) if a != b]
    if changed != found or len(staged) != len(lines):
        raise ValueError('staging may change only the root_url line')
    return '\n'.join(staged).encode('utf-8')


def published_tag(registration, snapshot, name):
    """The bottle tag a published asset name claims, or BottleConflict."""
    c, identity = inputs(registration, snapshot)
    prefix = f"{c['identifiers']['brew']}-{identity['version']}."
    suffix = '.bottle.tar.gz'
    tag = name[len(prefix):-len(suffix)] if (type(name) is str and name.startswith(prefix)
                                              and name.endswith(suffix)) else None
    try:
        bottle_tag_platform(tag)
    except BottleInvalid:
        raise BottleConflict(f'published release asset {name!r} is not a bottle of this version') from None
    if remote_filename(c['identifiers']['brew'], identity['version'], tag) != name:
        raise BottleConflict(f'published release asset {name!r} is not a canonical bottle filename')
    return tag


def select_bottles(registration, snapshot, github):
    """Unprivileged read of the published tap release for <app>-<version>.

    No release -> None. A complete release whose every asset verifies is selected
    as is (bottles need not be byte-reproducible). Anything else published is a
    BottleConflict: nothing is rebuilt or replaced.
    """
    c, identity = inputs(registration, snapshot)
    release = github.published_release(registration, identity['version'])
    if release is None:
        return None
    if (type(release) is not dict or release.get('tag') != release_tag(c, identity)
            or release.get('draft') is not False or type(release.get('assets')) is not list):
        raise BottleConflict('published release identity mismatch for ' + release_tag(c, identity))
    names = [asset.get('name') if type(asset) is dict else None for asset in release['assets']]
    if len(names) != len(BREW_PLATFORMS) or len(set(names)) != len(names):
        raise BottleConflict(f"published release {release_tag(c, identity)} has assets {sorted(map(str, names))}, "
                             f'not exactly one bottle per Brew platform')
    archives, verified = {}, []
    for name in names:
        tag = published_tag(registration, snapshot, name)
        data = github.download_bottle(registration, identity['version'], name)
        try:
            verified.append(verify_bottle(data, registration, snapshot, tag))
        except BottleInvalid as error:
            raise BottleConflict(f'published bottle {name} is invalid: {error}') from None
        except BottleConflict as error:
            raise BottleConflict(f'published bottle {name}: {error}') from None
        archives[name] = data
    try:
        selected = bottle_set(registration, snapshot, verified, canonical_root_url(c, identity))
    except BottleInvalid as error:
        raise BottleConflict(f'published release is incomplete: {error}') from None
    return dict(source='published', bottle_set=selected, archives=archives)


# native-checks.json, written by the native-checks summary job (T7) and read by the publisher.
MANAGER_PLATFORMS = {'brew': BREW_PLATFORMS, 'scoop': ('windows-amd64', 'windows-arm64'),
                     'winget': ('windows-amd64', 'windows-arm64')}
OUTCOMES = {'passed', 'failed', 'statically-mapped'}
STEP_STATES = {'passed', 'failed', 'skipped', 'not-run'}
STEPS = ('validate', 'install', 'version', 'uninstall')


class NativeEvidenceInvalid(ValueError):
    """native-checks.json is missing, malformed or has unknown fields."""


def _text(value, name, limit=1024):
    if type(value) is not str or len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise NativeEvidenceInvalid(f'invalid {name}')
    return value


def _exact(value, names, where):
    if type(value) is not dict or set(value) != set(names.split()):
        raise NativeEvidenceInvalid(f'{where}: missing or unknown fields (expected {names})')


def validate_native_checks(document):
    """Strict schema v1 check; returns a deep copy. Bindings are checked by native_failures."""
    from .contracts import CONTROL_REPOSITORY, VERSION, positive_id
    _exact(document, 'schema_version app version snapshot_digest configuration_digest candidates brew '
           'results run', 'native-checks')
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise NativeEvidenceInvalid('unsupported native-checks schema_version')
    if not matches(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', document['app']) or not matches(VERSION, document['version']):
        raise NativeEvidenceInvalid('invalid native-checks app or version')
    for name in ['snapshot_digest', 'configuration_digest']:
        if not matches(DIGEST, document[name]):
            raise NativeEvidenceInvalid('invalid ' + name)
    _exact(document['candidates'], 'scoop winget', 'candidates')
    if not all(matches(DIGEST, v) for v in document['candidates'].values()):
        raise NativeEvidenceInvalid('invalid candidate digest')
    brew = document['brew']
    if brew is not None:
        _exact(brew, 'source bottle_set identity', 'brew')
        if brew['source'] not in {'published', 'fresh'}:
            raise NativeEvidenceInvalid('brew source must be published or fresh')
        if type(brew['bottle_set']) is not dict:
            raise NativeEvidenceInvalid('brew bottle_set must be an object')
        _exact(brew['identity'], 'formula_sha256 bottle_set_sha256 snapshot_digest configuration_digest',
               'brew identity')
        if not all(matches(DIGEST, v) for v in brew['identity'].values()):
            raise NativeEvidenceInvalid('invalid brew identity digest')
    results = document['results']
    if type(results) is not list or len(results) > 32:
        raise NativeEvidenceInvalid('results must be a bounded list')
    seen = set()
    for entry in results:
        _exact(entry, 'manager platform outcome reason runner image image_version manager_version steps binding',
               'result')
        if entry['manager'] not in MANAGER_PLATFORMS or entry['platform'] not in MANAGER_PLATFORMS[entry['manager']]:
            raise NativeEvidenceInvalid('unsupported manager/platform result')
        key = (entry['manager'], entry['platform'])
        if key in seen:
            raise NativeEvidenceInvalid('duplicate result for ' + '/'.join(key))
        seen.add(key)
        if entry['outcome'] not in OUTCOMES:
            raise NativeEvidenceInvalid('invalid outcome')
        for name in ['reason', 'runner', 'image', 'image_version', 'manager_version']:
            _text(entry[name], name)
        _exact(entry['steps'], ' '.join(STEPS), 'steps')
        if not all(entry['steps'][step] in STEP_STATES for step in STEPS):
            raise NativeEvidenceInvalid('invalid step outcome')
        if entry['outcome'] == 'passed' and any(entry['steps'][s] != 'passed' for s in STEPS):
            raise NativeEvidenceInvalid('passed result with a step that did not pass')
        if entry['outcome'] == 'statically-mapped' and any(entry['steps'][s] != 'not-run' for s in STEPS):
            raise NativeEvidenceInvalid('statically-mapped result with executed steps')
        if not matches(DIGEST, entry['binding']):
            raise NativeEvidenceInvalid('invalid result binding')
    run = document['run']
    _exact(run, 'id attempt url', 'run')
    if not (positive_id(run['id']) and positive_id(run['attempt'])) or run['url'] != (
            f"https://github.com/{CONTROL_REPOSITORY}/actions/runs/{run['id']}/attempts/{run['attempt']}"):
        raise NativeEvidenceInvalid('invalid native-checks run reference')
    return copy.deepcopy(document)


def native_failures(channel, registration, snapshot, evidence, binding, context=None, bottles=None):
    """Failing platforms (or evidence problems) that stop one channel; [] means the gate passes.

    Brew: all four Brew platforms passed, bound to one bottled_formula_identity that
    the caller reproduced (binding = formula sha256, bottles = its bottle_set).
    Scoop/WinGet: windows-amd64 passed and windows-arm64 passed or statically
    mapped for no-hosted-runner, each bound to the re-rendered candidate digest.
    """
    if evidence is None:
        return ['native-checks.json missing']
    try:
        document = validate_native_checks(evidence)
    except NativeEvidenceInvalid as error:
        return ['native-checks.json invalid: ' + str(error)]
    c = registration['configuration']
    identity = snapshot['payload']['identity']
    if (document['app'], document['version'], document['snapshot_digest'], document['configuration_digest']) != (
            c['app'], identity['version'], snapshot['snapshot_digest'], registration['configuration_digest']):
        return ['native-checks.json bound to a different app, version, snapshot or configuration']
    if context is not None and not (document['run']['id'] == context['run_id']
                                    and document['run']['attempt'] <= context['attempt']):
        return ['native-checks.json belongs to a different workflow run']
    if channel == 'brew':
        brew = document['brew']
        if brew is None:
            return ['brew evidence missing']
        if bottles is None or brew['bottle_set'] != bottles:
            return ['brew evidence checked a different bottle_set']
        expected = bottled_formula_identity(registration, snapshot, bottles)
        if brew['identity'] != expected or binding != expected['formula_sha256']:
            return ['brew evidence bound to a different bottled formula identity']
    elif document['candidates'][channel] != binding:
        return [f'{channel} evidence bound to a different candidate digest']
    results = {entry['platform']: entry for entry in document['results'] if entry['manager'] == channel}
    failing = []
    for platform in MANAGER_PLATFORMS[channel]:
        entry = results.get(platform)
        if entry is None:
            failing.append(platform + ': missing')
        elif entry['binding'] != binding:
            failing.append(platform + ': bound to a different definition')
        elif entry['outcome'] == 'passed':
            continue
        elif (entry['outcome'] == 'statically-mapped' and platform == 'windows-arm64'
              and entry['reason'] == 'no-hosted-runner'):
            continue
        else:
            failing.append(f"{platform}: {entry['outcome']}" + (f" ({entry['reason']})" if entry['reason'] else ''))
    return failing
