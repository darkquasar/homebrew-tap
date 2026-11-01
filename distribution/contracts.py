"""Strict JSON contracts; persisted approvals must come from protected control."""
import copy
import hashlib
import json
import re

OWNER_ID = 28435534
OWNER_LOGIN = 'darkquasar'
CONTROL_REPOSITORY = 'darkquasar/homebrew-tap'
CONTROL_BRANCH = 'distribution-control'
RECORDS_BRANCH = 'distribution-records'
SHA = r'[0-9a-f]{40}'
DIGEST = r'[0-9a-f]{64}'
VERSION = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()),
            'missing or unknown fields: ' + names)


def canonical_digest(document):
    """Hash the explicit payload only. No implicit exclusion or coercion."""
    def check(value):
        if type(value) is dict:
            require(all(type(k) is str for k in value), 'JSON keys must be strings')
            for child in value.values():
                check(child)
        elif type(value) is list:
            for child in value:
                check(child)
        else:
            require(type(value) in (str, int, bool, type(None)), 'non-canonical JSON value')
    check(document)
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def validate_registration(document):
    fields(document, 'schema_version configuration configuration_digest approval')
    require(type(document['schema_version']) is int and document['schema_version'] == 1,
            'unsupported registration schema')
    config = document['configuration']
    validate_configuration(config)
    digest = canonical_digest(config)
    approval = document['approval']
    fields(approval, 'owner_id reference configuration_digest')
    require(approval['owner_id'] == OWNER_ID and type(approval['owner_id']) is int,
            'only owner approval is permitted')
    require(type(approval['reference']) is str and bool(approval['reference'].strip()),
            'approval reference required')
    require(document['configuration_digest'] == digest == approval['configuration_digest'],
            'configuration approval digest mismatch')
    require(config['destinations']['brew']['path'] == f"Formula/{config['app']}.rb",
            'formula path must belong to registered app')
    return copy.deepcopy(document)


def matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def positive_id(value):
    return type(value) is int and value > 0


def validate_configuration(config):
    fields(config, 'app repository owner_login owner_id history_branch tag_pattern '
           'dispatcher_app_id dispatcher_actor_id release_workflow_path homepage '
           'description license identifiers assets destinations')
    app = config['app']
    require(matches(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', app), 'invalid app')
    require(config['owner_login'] == OWNER_LOGIN and config['owner_id'] == OWNER_ID,
            'unapproved owner')
    require(matches(r'darkquasar/[A-Za-z0-9][A-Za-z0-9_.-]*', config['repository']),
            'repository must belong to owner')
    require(matches(r'[A-Za-z0-9][A-Za-z0-9_/-]*', config['history_branch'])
            and '//' not in config['history_branch'] and not config['history_branch'].endswith('/'),
            'invalid history branch')
    require(config['tag_pattern'] == 'vMAJOR.MINOR.PATCH', 'unsupported tag policy')
    require(positive_id(config['dispatcher_app_id']) and positive_id(config['dispatcher_actor_id'])
            and config['dispatcher_actor_id'] != OWNER_ID, 'invalid dispatcher identity')
    require(matches(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml', config['release_workflow_path']),
            'invalid workflow path')
    for key in ['homepage', 'description', 'license']:
        require(type(config[key]) is str and 0 < len(config[key]) <= 2048
                and not any(ord(ch) < 32 for ch in config[key]), 'invalid ' + key)
    require(config['homepage'].startswith('https://'), 'HTTPS homepage required')
    identifiers = config['identifiers']
    fields(identifiers, 'brew scoop winget command')
    require(identifiers['brew'] == app == identifiers['scoop'] == identifiers['command'],
            'native names must belong to app')
    require(matches(r'darkquasar\.[A-Z][A-Za-z0-9]*', identifiers['winget'])
            and identifiers['winget'].split('.')[1].lower() == app.replace('-', ''),
            'WinGet identity must belong to app')
    assets = config['assets']
    fields(assets, 'darwin-amd64 darwin-arm64 linux-amd64 linux-arm64 windows-amd64 windows-arm64')
    require(all(matches(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) and '..' not in name
                and name != 'checksums.txt' for name in assets.values()), 'unsafe asset name')
    require(len(set(assets.values())) == 6, 'duplicate asset names')
    if app == 'patronus':
        require(config['repository'] == 'darkquasar/patronus', 'Patronus source is fixed')
        require(all(name == 'patronus-' + arch + ('.exe' if arch.startswith('windows') else '')
                    for arch, name in assets.items()), 'Patronus asset contract is fixed')
    destinations = config['destinations']
    fields(destinations, 'brew scoop winget')
    expected = {
        'brew': ('darkquasar/homebrew-tap', f'Formula/{app}.rb'),
        'scoop': ('darkquasar/scoop-bucket', f'bucket/{app}.json'),
        'winget': ('darkquasar/winget-pkgs', 'manifests/d/' + identifiers['winget'].replace('.', '/')),
    }
    for channel, (repository, path) in expected.items():
        fields(destinations[channel], 'repository path')
        require(destinations[channel] == {'repository':repository, 'path':path},
                'destination outside registered app namespace')
