"""Deterministic Homebrew formula renderer. Never executes release contents."""
import json
import re

from .validation import asset_url, inputs


def ruby_string(value):
    return re.sub(r'#(?=[{@$])', r'\\#', json.dumps(value, ensure_ascii=False))


def render(registration, snapshot):
    c, identity = inputs(registration, snapshot)
    command = c['identifiers']['command']
    stanzas = []
    installs = []
    for system, condition in [('darwin', 'macos'), ('linux', 'linux')]:
        stanzas.append(f'  on_{condition} do')
        installs.append('    ' + ('if' if system == 'darwin' else 'elsif') + f' OS.{"mac" if system == "darwin" else "linux"}?')
        for cpu, native in [('amd64', 'intel'), ('arm64', 'arm')]:
            asset = c['assets'][system + '-' + cpu]
            stanzas.extend([
                f'    on_{native} do',
                f'      url {ruby_string(asset_url(c, identity, asset))}, using: :nounzip',
                f'      sha256 {ruby_string(identity["assets"][asset]["sha256"])}',
                '    end'])
            if cpu == 'amd64':
                stanzas.append('')
            installs.extend([
                '      ' + ('if' if cpu == 'amd64' else 'else') + (' Hardware::CPU.intel?' if cpu == 'amd64' else ''),
                f'        bin.install {ruby_string(asset)} => {ruby_string(command)}'])
        stanzas.append('  end')
        installs.append('      end')
        if system == 'darwin':
            stanzas.append('')
    installs.append('    end')
    text = f'''class {''.join(part.capitalize() for part in c['app'].split('-'))} < Formula
  desc {ruby_string(c['description'])}
  homepage {ruby_string(c['homepage'])}
  version {ruby_string(identity['version'])}
  license {ruby_string(c['license'])}

{chr(10).join(stanzas)}

  def install
    supported = (OS.mac? || OS.linux?) && Hardware::CPU.is_64_bit? &&
                (Hardware::CPU.intel? || Hardware::CPU.arm?)
    odie "Unsupported operating system or CPU architecture" unless supported

{chr(10).join(installs)}
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/{command} --version")
  end
end
'''
    return {c['destinations']['brew']['path']: text.encode()}
