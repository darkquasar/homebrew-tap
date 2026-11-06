"""WinGet 1.10.0 multi-file portable manifests; preparation only."""
import yaml

from .validation import asset_url, inputs


def render(registration, snapshot):
    c, identity = inputs(registration, snapshot)
    package = c['identifiers']['winget']
    common = dict(PackageIdentifier=package, PackageVersion=identity['version'])
    installers = []
    for native, cpu in [('x64', 'amd64'), ('arm64', 'arm64')]:
        asset = c['assets']['windows-' + cpu]
        installers.append(dict(Architecture=native, InstallerUrl=asset_url(c, identity, asset),
                               InstallerSha256=identity['assets'][asset]['sha256'].upper()))
    documents = {
        '': dict(common, DefaultLocale='en-US', ManifestType='version'),
        '.installer': dict(common, InstallerType='portable',
            Commands=[c['identifiers']['command']], Installers=installers, ManifestType='installer'),
        '.locale.en-US': dict(common, PackageLocale='en-US', Publisher=c['owner_login'],
            PackageName=package.split('.')[1], PackageUrl=c['homepage'], License=c['license'],
            ShortDescription=c['description'], ManifestType='defaultLocale'),
    }
    directory = c['destinations']['winget']['path'] + '/' + identity['version']
    result = {}
    for suffix, document in documents.items():
        document['ManifestVersion'] = '1.10.0'
        header = ('# yaml-language-server: $schema=https://aka.ms/'
                  f"winget-manifest.{document['ManifestType']}.1.10.0.schema.json\n")
        result[f'{directory}/{package}{suffix}.yaml'] = (header + yaml.safe_dump(document,
            allow_unicode=True, sort_keys=False, width=1000)).encode()
    return result
