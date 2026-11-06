"""Public JSON results, byte digests and bounded compare-and-swap updates."""
import hashlib
import json
from datetime import datetime, timezone

from .contracts import VERSION, canonical_digest, matches, require


def encode(document):
    canonical_digest(document)
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def file_digest(candidate):
    return canonical_digest({p: hashlib.sha256(b).hexdigest() for p, b in sorted(candidate.items())})


def version_key(version):
    require(matches(VERSION, version), 'invalid stable version')
    return tuple(map(int, version.split('.')))


def result(channel, registration, snapshot, **changes):
    value = dict(schema_version=1, app=registration['configuration']['app'],
        version=snapshot['payload']['identity']['version'], channel=channel, state='generated',
        snapshot_digest=snapshot['snapshot_digest'], configuration_digest=registration['configuration_digest'],
        definition_digest=None, validation=None, destination_commit=None, refs={},
        observed_at=datetime.now(timezone.utc).isoformat(), failure_code=None, message='')
    value.update(changes)
    return value


def regular(tree, path):
    """An absent entry is optional; a symlink, tree or symlink parent is never absent."""
    parts = path.split('/')
    for index in range(1, len(parts)):
        require('/'.join(parts[:index]) not in tree, 'non-directory destination parent')
    entry = tree.get(path)
    if entry is None:
        return None
    require(entry['type'] == 'file' and type(entry['content']) is bytes,
            'destination must be a regular file')
    return entry['content']


def save_status(registration, snapshot, updates, github):
    """Merge only this operation's channel results after every conflicting tip."""
    from .contracts import CONTROL_REPOSITORY
    from .snapshot import strict_json
    app = registration['configuration']['app']
    version = snapshot['payload']['identity']['version']
    path = f'.distribution/status/{app}/{version}.json'
    branch = github.default_branch(CONTROL_REPOSITORY)
    require(branch not in {'distribution-control', 'distribution-records'}, 'native default branch not commissioned')
    for _ in range(5):
        tip = github.branch_tip(CONTROL_REPOSITORY, branch)
        require(tip is not None, 'status branch unavailable')
        raw = regular(github.tree(CONTROL_REPOSITORY, tip, paths=[path]), path)
        recovered = None
        try:
            prior = strict_json(raw) if raw else {}
            channels = prior.get('channels', {})
            require(type(channels) is dict, 'invalid status cache')
        except Exception as error:
            prior,channels = {},{}
            recovered = dict(previous_bytes_sha256=hashlib.sha256(raw).hexdigest(),message=str(error))
        document = dict(schema_version=1, app=app, version=version,
                        channels={**channels, **updates})
        if recovered or prior.get('cache_recovery'):
            document['cache_recovery'] = recovered or prior['cache_recovery']
        if raw == encode(document):
            return tip
        commit = github.cas(CONTROL_REPOSITORY, branch, tip, {path: encode(document)})
        if commit:
            return commit
    raise ValueError('status cas-exhausted; destination markers retained for recovery')
