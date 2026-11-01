"""Release byte inspection and fixed identity. This module never writes records."""
import copy
import hashlib
import json
import re

from .contracts import (OWNER_ID, SHA, DIGEST, VERSION, canonical_digest, fields, matches,
                        require, validate_registration)


def strict_json(data):
    def unique(pairs):
        result = {}
        for key,value in pairs:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    result = json.loads(data, object_pairs_hook=unique)
    canonical_digest(result)  # Reject floats/non-finite values, including nested ones.
    return result


def peel_tag(github, repository, tag):
    obj = github.ref(repository, 'tags/' + tag)
    visited = set()
    for _ in range(16):
        require(type(obj) is dict and matches(SHA,obj.get('sha')), 'invalid tag object')
        if obj.get('type') == 'commit':
            return obj['sha']
        require(obj.get('type') == 'tag' and obj['sha'] not in visited, 'invalid/cyclic tag')
        visited.add(obj['sha'])
        obj = github.tag(repository,obj['sha'])
    raise ValueError('tag dereference bound exceeded')


def inspect_identity(registration, tag, github):
    c = validate_registration(registration)['configuration']
    require(matches('v' + VERSION,tag), 'ineligible stable tag')
    repository = c['repository']
    repo = github.repository(repository)
    require(repo.get('full_name') == repository and repo.get('owner_id') == OWNER_ID,
            'wrong source repository/owner')
    commit = peel_tag(github,repository,tag)
    branch = github.ref(repository,'heads/' + c['history_branch'])
    require(branch.get('type') == 'commit' and matches(SHA,branch.get('sha'))
            and github.is_ancestor(repository,commit,branch['sha']) is True,
            'release is outside approved history')
    release = github.release(repository,tag)
    require(release.get('tag_name') == tag and release.get('draft') is False
            and release.get('prerelease') is False, 'not a published stable release')
    names = set(c['assets'].values())
    metadata = {}
    require(type(release.get('assets')) is list, 'asset listing missing')
    for asset in release['assets']:
        name = asset.get('name')
        require(type(name) is str and name not in metadata, 'duplicate/invalid release asset')
        metadata[name] = asset
    require(names | {'checksums.txt'} <= set(metadata), 'required assets missing')
    checksums = github.download_asset(repository,tag,'checksums.txt')
    require(type(checksums) is bytes and 0 < len(checksums) <= 1024*1024, 'invalid checksum file')
    require(metadata['checksums.txt'].get('size') == len(checksums), 'checksum file size mismatch')
    hashes = {}
    for line in checksums.decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *]([^\r\n]+)',line)
        require(match is not None, 'malformed checksum line')
        digest,name = match.groups()
        require(name not in hashes, 'duplicate checksum entry')
        hashes[name] = digest.lower()
    require(set(hashes) == names, 'checksum set must match all required assets')
    assets = {}
    for name in sorted(names):
        data = github.download_asset(repository,tag,name)
        require(type(data) is bytes and len(data) > 0, 'empty or unreadable release asset')
        digest = hashlib.sha256(data).hexdigest()
        require(digest == hashes[name] and metadata[name].get('size') == len(data),
                'release checksum or size mismatch')
        assets[name] = {'size':len(data),'sha256':digest}
    return dict(app=c['app'],repository=repository,tag=tag,version=tag[1:],commit=commit,
                checksums_sha256=hashlib.sha256(checksums).hexdigest(),assets=assets)


def freeze(registration, identity, authorization):
    payload = dict(identity=copy.deepcopy(identity),authorization=copy.deepcopy(authorization),
                   initial_configuration_digest=registration['configuration_digest'])
    return {'schema_version':1,'payload':payload,'snapshot_digest':canonical_digest(payload)}


def approve_snapshot(registration, snapshot, approval):
    registration = validate_registration(registration)
    fields(approval, 'owner_id triggering_actor_id mode role controller_sha '
           'configuration_digest identity_digest reference')
    require(approval['owner_id'] == OWNER_ID == approval['triggering_actor_id']
            and approval['mode'] == 'approve-baseline' and approval['role'] == 'authority',
            'owner baseline admission required')
    require(matches(SHA,approval['controller_sha'])
            and approval['configuration_digest'] == registration['configuration_digest'],
            'baseline admission/configuration mismatch')
    require(type(approval['reference']) is str and bool(approval['reference'].strip()),
            'authenticated owner reference required')
    validate_identity(registration,snapshot)
    digest = canonical_digest(snapshot)
    require(digest == approval['identity_digest'], 'baseline differs from owner-approved bytes')
    return freeze(registration,snapshot,dict(kind='baseline',reference=approval['reference'],
                  owner_id=OWNER_ID,identity_digest=digest))


def load_authorized_snapshot(registration, record_ref, github):
    """A locator is not authority: repeat admission, protected reads and byte checks."""
    from .contracts import CONTROL_REPOSITORY, RECORDS_BRANCH
    from .trust import authorize_controller, commit_ref

    registration = validate_registration(registration)
    c = registration['configuration']
    fields(record_ref, 'schema_version app version controller_sha configuration_digest records_commit snapshot_digest')
    require(type(record_ref['schema_version']) is int and record_ref['schema_version'] == 1,
            'unsupported record reference')
    require(record_ref['app'] == c['app'] and matches(VERSION,record_ref['version'])
            and matches(SHA,record_ref['records_commit']) and matches(DIGEST,record_ref['snapshot_digest']),
            'invalid record locator')
    admission = authorize_controller(registration,github.controller_context(),github)
    require(admission['role'] == 'publish' and admission['mode'] in {'prepare','approve-baseline','retry'}
            and record_ref['controller_sha'] == admission['controller_sha']
            and record_ref['configuration_digest'] == admission['configuration_digest'],
            'wrong role, stale or forged record reference')
    tip = commit_ref(github,CONTROL_REPOSITORY,'heads/' + RECORDS_BRANCH)
    require(github.is_ancestor(CONTROL_REPOSITORY,record_ref['records_commit'],tip) is True,
            'record commit not reachable from protected records')
    path = f".distribution/releases/{c['app']}/{record_ref['version']}.json"
    documents = []
    for commit in [record_ref['records_commit'],tip]:
        entry = github.file(CONTROL_REPOSITORY,commit,path)
        require(entry.get('type') == 'file' and type(entry.get('content')) is bytes
                and len(entry['content']) <= 1024*1024, 'record must be a bounded regular file')
        documents.append(strict_json(entry['content']))
    frozen,current = documents
    require(frozen == current, 'protected release record changed')
    validate_snapshot(registration,frozen)
    require(frozen['snapshot_digest'] == record_ref['snapshot_digest']
            and frozen['payload']['identity']['version'] == record_ref['version'],
            'snapshot locator mismatch')
    original_digest = frozen['payload']['initial_configuration_digest']
    revision = github.registration_revision(c['app'],original_digest,admission['controller_sha'])
    require(matches(SHA,revision.get('commit'))
            and github.is_ancestor(CONTROL_REPOSITORY,revision['commit'],admission['controller_sha']) is True,
            'initial registration not in protected control history')
    original = validate_registration(revision['registration'])
    require(original['configuration_digest'] == original_digest, 'initial configuration digest mismatch')
    # Metadata corrections are supported; old bytes cannot acquire a different public identity/scope.
    presentation = {'homepage','description','license','dispatcher_app_id','dispatcher_actor_id'}
    scope = lambda config: {k:v for k,v in config.items() if k not in presentation}
    require(scope(original['configuration']) == scope(c), 'frozen release is outside current approved scope')
    require(frozen['payload']['identity'] == inspect_identity(registration,'v'+record_ref['version'],github),
            'frozen identity conflicts with current release')
    return copy.deepcopy(frozen)


def validate_identity(registration, identity):
    c = validate_registration(registration)['configuration']
    fields(identity, 'app repository tag version commit checksums_sha256 assets')
    require(identity['app'] == c['app'] and identity['repository'] == c['repository'],
            'identity outside registered source')
    require(matches(VERSION,identity['version']) and identity['tag'] == 'v'+identity['version']
            and matches(SHA,identity['commit']) and matches(DIGEST,identity['checksums_sha256']),
            'invalid release identity')
    require(type(identity['assets']) is dict and set(identity['assets']) == set(c['assets'].values()),
            'identity asset scope mismatch')
    for asset in identity['assets'].values():
        fields(asset, 'size sha256')
        require(type(asset['size']) is int and asset['size'] > 0 and matches(DIGEST,asset['sha256']),
                'invalid asset identity')


def validate_snapshot(registration, document):
    fields(document, 'schema_version payload snapshot_digest')
    require(type(document['schema_version']) is int and document['schema_version'] == 1,
            'unsupported snapshot schema')
    payload = document['payload']
    fields(payload, 'identity authorization initial_configuration_digest')
    validate_identity(registration,payload['identity'])
    require(matches(DIGEST,payload['initial_configuration_digest'])
            and document['snapshot_digest'] == canonical_digest(payload), 'snapshot digest mismatch')
    authorization = payload['authorization']
    fields(authorization, 'kind reference owner_id identity_digest')
    require(authorization['owner_id'] == OWNER_ID
            and authorization['identity_digest'] == canonical_digest(payload['identity']),
            'snapshot authority mismatch')
    if authorization['kind'] == 'baseline':
        require(type(authorization['reference']) is str and bool(authorization['reference'].strip()),
                'missing baseline approval reference')
    else:
        require(authorization['kind'] == 'release-run', 'unknown authorization kind')
        reference = authorization['reference']
        fields(reference, 'repository run_id attempt artifact_id artifact_sha256 workflow_path workflow_revision workflow_sha256')
        from .contracts import positive_id
        require(reference['repository'] == payload['identity']['repository']
                and reference['workflow_revision'] == payload['identity']['commit']
                and reference['workflow_path'] == registration['configuration']['release_workflow_path']
                and all(positive_id(reference[k]) for k in ['run_id','attempt','artifact_id'])
                and matches(DIGEST,reference['artifact_sha256']) and matches(DIGEST,reference['workflow_sha256']),
                'invalid original release authorization')
    return copy.deepcopy(document)
