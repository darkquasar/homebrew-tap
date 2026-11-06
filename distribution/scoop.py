"""Scoop manifests contain only binaries and architecture-local command aliases."""
import json

from .validation import asset_url, inputs


def render(registration, snapshot):
    c, identity = inputs(registration, snapshot)
    manifest = dict(version=identity['version'], description=c['description'],
                    homepage=c['homepage'], license=c['license'], architecture={})
    for native, cpu in [('64bit', 'amd64'), ('arm64', 'arm64')]:
        asset = c['assets']['windows-' + cpu]
        manifest['architecture'][native] = dict(url=asset_url(c, identity, asset),
            hash=identity['assets'][asset]['sha256'], bin=[[asset, c['identifiers']['command']]])
    data = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    return {c['destinations']['scoop']['path']: data}
