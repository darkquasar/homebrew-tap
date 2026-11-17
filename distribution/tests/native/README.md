# Native executable-package lifecycle procedure

**All lifecycle rows below are PENDING.** This is a manual procedure and inert
invented preservation data, not a runnable test harness or recorded native pass.
Run it only with a separate native/commissioning grant, on disposable supported
hosts/users and isolated catalogue copies. It concerns executable packages
(Brew/Scoop/WinGet), not Patronus content packages. Never invoke Patronus content
install/update/remove commands as a package lifecycle hook.

## Scope and prior observations

Record exact source/control/producer SHAs, native candidate and snapshot hashes,
OS/build, actual CPU and process architecture, manager/client versions, runner
image/revision and ordinary-user privileges. Supported mappings are Brew macOS
and Linux amd64/arm64, Scoop/WinGet Windows amd64/arm64; x86 is unsupported.
Do not call a static mapping or cross-build native execution.

Prior recorded observations use synthetic binaries: macOS ARM64 and Linux ARM64
install/discovery/version, plus Windows x64 run
[42178401448](https://github.com/darkquasar/patronus/actions/runs/42178401448)
with a verified non-admin user, Developer Mode off, no symlink privilege and
RemoteSigned policy. It observed strict WinGet 1.10 validation, default per-user
Patronus and explicit per-user Lumen, Scoop shims/User PATH and checksum rejection.
Its Scoop removal freed names for WinGet; it does not prove full composed
upgrade/uninstall/sentinel acceptance. Windows ARM, Linux amd64 native execution,
live release/source installs and the new Windows source-query job remain
pending. Retain the bounded existing producer Windows procedure; no new fleet,
recurring matrix, client bootstrap or permission-policy bypass is prescribed.

## Stage two apps, with no remote publication

Use accepted fixtures under `../fixtures/`: Patronus `2.7.1` and Lumen `1.4.2`,
different source repositories and dispatcher identities. The `*-native-registration`
files use synthetic presentation metadata; their approvals/IDs are not authority.
Install validation dependencies only through the separately approved tooling
setup and `validation-requirements.txt` hash lock; missing tools block the row.
From tap root, set `staging` to a fresh directory outside checkouts. Generate
native definitions with the real accepted API, preserving exact path/bytes:

```sh
python3 -B - "$staging" <<'PY'
from pathlib import Path
import sys
from distribution.snapshot import strict_json
from distribution.validation import render
root = Path(sys.argv[1])
for app in ('patronus', 'lumen'):
    fixture = Path('distribution/tests/fixtures')
    registration = strict_json((fixture / (app + '-native-registration.json')).read_bytes())
    snapshot = strict_json((fixture / (app + '-snapshot.json')).read_bytes())
    for channel in ('brew', 'scoop', 'winget'):
        for path, data in render(channel, registration, snapshot).items():
            for tree in (root / 'candidates' / app / channel, root / 'catalogues' / channel):
                target = tree / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
PY
python3 -B -m distribution.cli validate --channel scoop --app lumen \
  --registration distribution/tests/fixtures/lumen-native-registration.json \
  --snapshot distribution/tests/fixtures/lumen-snapshot.json \
  --candidate-dir "$staging/candidates/lumen/scoop" --out "$staging/lumen-scoop-validation.json"
```

Repeat with the matching `candidates/APP/CHANNEL` directory for `brew`/`winget`
and Patronus's registration/snapshot pair. Each validation root must contain
only that app/channel's exact expected paths; output JSON stays outside it.
The separate `catalogues/CHANNEL` trees combine both apps for discovery, not
for this strict per-candidate CLI. This CLI validates existing files, not generates
them. Every structural result reports `authority: not-evaluated`; retain actual
exit, output, tool/schema pins and candidate/configuration/snapshot digests.
Use qualified local formula/bucket discovery for both apps in the same isolated
catalogue; local WinGet schema/layout validation suffices for second-app staging.
Do not publish Lumen or open its upstream PR. Fixture URLs are invented inputs;
do not execute/download from them as if real release binaries. Native installation
requires separately staged, byte-verified synthetic binaries and the accepted
transport adaptation, or separately authorized actual release bytes.

Before native checks, inspect `ruby -c`/`brew style` and `brew test` on the actual
formula. On Windows, run `winget validate --manifest VERSION_DIRECTORY` for both
complete manifest sets against the recorded installed client/schema. Keep all
three official 1.10 headers. Portable manifests intentionally omit `Scope`; do
not insert it or ignore native warnings. Reject x86 rather than selecting x64.
Native/client disagreement returns to the owner before format/elevation changes.

## Local catalogue command shapes (separate native grant)

Use disposable manager prefixes/accounts with no existing `darkquasar` tap/bucket.
After independent byte verification and any accepted local transport adaptation,
commit the older candidate catalogue to an owner-controlled **local Git repo**.
Record its path/commit and the newer candidate commit before using these shapes.
For Homebrew, `local_brew_repo` is the local repository path (with `Formula/`):

```sh
brew tap darkquasar/tap "$local_brew_repo"
brew trust --formula darkquasar/tap/patronus
brew trust --formula darkquasar/tap/lumen
brew info darkquasar/tap/patronus
brew info darkquasar/tap/lumen
brew install darkquasar/tap/patronus
```

Use item trust only on supporting Brew versions, including Lumen when inspecting
it. Advance the local source through a normal newer-candidate commit, `brew update`,
then `brew upgrade darkquasar/tap/patronus`; retain all before/after commits and
exact versions. See [brew tap syntax](https://docs.brew.sh/Manpage).
Never repoint a real user's tap or use these public aliases outside isolation.

On the disposable Windows normal-user account, `scoopScript` points to the
already staged accepted Scoop `bin/scoop.ps1`, and `localBucketRepo` to a local
Git repository containing `bucket/` for both apps. Use the PowerShell entry
under normal policy, as in the accepted Windows procedure; do not use upstream
launchers that override policy:

```powershell
& $scoopScript bucket add darkquasar $localBucketRepo
& $scoopScript search patronus
& $scoopScript search lumen
& $scoopScript install darkquasar/patronus
# Advance the local Git source to the separately approved newer candidate.
& $scoopScript update
& $scoopScript update patronus
& $scoopScript uninstall patronus
```

For local WinGet manifests, an administrator enables `LocalManifestFiles` once
on that disposable host and records setup separately. The existing
[Windows procedure](https://github.com/darkquasar/patronus/blob/main/tools/tests/package-windows-validation.md)
uses this machine policy so the separate normal-user SID receives it:

```powershell
$localManifestPolicy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppInstaller'
New-Item -Path $localManifestPolicy -Force | Out-Null
New-ItemProperty -Path $localManifestPolicy -Name EnableLocalManifestFiles -Value 1 -PropertyType DWord -Force | Out-Null
Get-ItemProperty -Path $localManifestPolicy -Name EnableLocalManifestFiles
```

This host setup is not a consumer install step or a change to PowerShell
permission policy. Then, as the verified **normal user**, `wingetExe` points to
the recorded supported client and `oldManifest`/`newManifest` to complete,
byte-verified older/newer version directories:

```powershell
& $wingetExe validate --manifest $oldManifest
& $wingetExe validate --manifest $newManifest
& $wingetExe install --manifest $oldManifest --architecture x64 --accept-package-agreements --accept-source-agreements --disable-interactivity
# Verify old version/hash/PATH and every preservation sentinel before upgrade.
& $wingetExe upgrade --manifest $newManifest --architecture x64 --accept-package-agreements --accept-source-agreements --disable-interactivity
& $wingetExe uninstall --id darkquasar.Patronus --exact
```

Substitute the actual supported host architecture, never x64 fallback for ARM/x86.
Retain default per-user Patronus installation; the accepted Lumen example also
checks explicit `--scope user`. `Scope` remains absent from portable manifests.
See Microsoft's [local install](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)
and [upgrade syntax](https://learn.microsoft.com/en-us/windows/package-manager/winget/upgrade).
Local-manifest identity can differ from the default source: these shapes do not
prove default-source upgrade. Live-source lifecycle needs both versions indexed;
otherwise that row remains pending. No new runnable harness or actual native
operation is added by this procedure.

## Preservation setup

Use a new disposable user home and project. Each seeded fixture target must be
absent before copying; retain unrelated files created by host/manager setup.
Copy the **contents of `preservation/project/`** into the disposable project root
and the **contents of `preservation/home/`** into that disposable user's home.
Do not copy the wrapper `preservation/`, the `project/` or `home/` directory itself,
or `manifest.json` into either target. Keep the manifest with the external
baseline/evidence. Never overlay real user files or invoke content commands.
For Unix, with `native_fixture_source` pointing to this procedure's
`preservation/` directory and the two target variables pointing to the disposable
roots:

```sh
cp -R "$native_fixture_source/project/." "$fixture_project/"
cp -R "$native_fixture_source/home/." "$fixture_home/"
```

On Windows copy each root's children with `Get-ChildItem -Force` and
`Copy-Item -Recurse`, including hidden directories. Default home/project paths:

| Sentinel | Project-relative target | Disposable-home-relative target |
|---|---|---|
| Patronus state | `.patronus/state.json` | `.patronus/state.json` |
| Claude settings / instructions | `.claude/settings.json`, `CLAUDE.md` | `.claude/settings.json`, `.claude/CLAUDE.md` |
| Codex configuration / instructions | `.codex/config.toml`, `AGENTS.md` | `.codex/config.toml`, `.codex/AGENTS.md` |
| OpenCode configuration / instructions | `opencode.json`, `AGENTS.md` | `.config/opencode/opencode.json`, `.config/opencode/AGENTS.md` |
| Pi configuration / instructions | `.pi/settings.json`, `AGENTS.md` | `.pi/agent/settings.json`, `.pi/agent/AGENTS.md` |
| Managed content / user content | `.claude/skills/invented-preservation/SKILL.md`, `content-package/data.txt` | Same relative paths |
| Unrelated bytes | `unrelated.txt` | `unrelated.txt` |

Paths match the accepted adapters' default roots. Record any actual Codex,
OpenCode or Pi root override and map the sentinels to those disposable effective
roots before checking; do not alter a real user's environment. The JSON/TOML
sentinels contain invented data, no keys/hooks/programs. State records have their
matching scope (`local`/`global`) and a checksum tied to the invented skill;
they are preservation data, never an approval or native result.

Keep byte-for-byte copies of the seeded fixture trees outside both locations,
with sorted target-relative path/SHA-256 inventories. Compare complete seeded
content directories to catch unexpected additions as well as missing files;
record manager-owned paths/cache changes separately from preservation targets. Also save the disposable User and Machine PATH values and
resolve every `patronus` command before operations. Choose no existing real
manual installation; an optional inert manual-copy sentinel must live at a
separate recorded path. Record both its bytes and the expected PATH priority.

For Unix, an external recursive comparison covers file absence/addition as well
as bytes (`fixture_before` is the saved project baseline; `fixture_project` is
the disposable project):

```sh
diff -r "$fixture_before" "$fixture_project"
command -v patronus
type -a patronus
```

For Windows, inventory all relative paths within the seeded preservation targets
and compare sorted SHA-256 hashes in PowerShell, including hidden files with
`Get-ChildItem -Recurse -Force -File`.
Use `Compare-Object` on both path/hash inventories and inspect missing/additional
paths as failures, not just matching existing files. Repeat for the seeded
disposable-home targets; do not treat expected manager-owned cache changes as
preservation failures.
Record resolved commands with `Get-Command patronus -All` in a **fresh** normal
user process using persisted User/Machine PATH. Keep normal permission checks
and the recorded PowerShell policy. A reused process PATH is insufficient.

## Lifecycle steps and evidence

For each manager on the authorized representative host:

1. Install the approved older stable candidate, record actual command/exit,
   resolved executable path/hash and exact `patronus --version`. Brew uses
   `brew install darkquasar/tap/patronus` after tap/item trust; Scoop uses
   `scoop install darkquasar/patronus` after bucket setup; live WinGet uses
   `winget install --id darkquasar.Patronus --exact --source winget` only after
   exact default-source availability. Staging must use separately recorded local
   catalogue/manifest paths instead of pretending those public commands worked.
2. Compare all home/project preservation paths, contents and optional manual
   sentinel. Verify no content command/harness hook ran and no user state was
   erased. Preserve unrelated PATH entries; manager-owned additions are expected.
3. Advance the isolated catalogue to a separately authorized newer candidate
   with frozen URLs/hashes. Run `brew update` then
   `brew upgrade darkquasar/tap/patronus`; or `scoop update` then
   `scoop update patronus`; or live
   `winget upgrade --id darkquasar.Patronus --exact --source winget`.
   Fresh-shell resolution must report the newer exact version/hash without
   building Patronus. Recheck all sentinels. Do not claim upgrade when installing
   the same version twice.
4. Run `brew uninstall darkquasar/tap/patronus`, `scoop uninstall patronus`, or
   `winget uninstall --id darkquasar.Patronus --exact`. Confirm its binary/manager
   links or shims are absent; check all command resolutions. A separately
   recorded manual installation may remain and must not be deleted to make the
   absence check pass. Recheck every preservation file, directory listing and
   unrelated PATH entry. Keep manager metadata changes separate from user data.
5. Retain actual outputs/exits, candidate hashes, before/after path/hash
   inventories and any failures/limitations. Do not remove the host, evidence or
   staged source automatically. A failing/unrun row stays failed/pending.

```text
+---------------+  => manager install  +---------------+  => manager upgrade  +---------------+
| Saved baseline| ===================> | Older binary  | ===================> | Newer binary  |
+---------------+                      +---------------+                      +---------------+
                                                                                     | => uninstall
                                                                                     v
                                                                              +---------------+
                                                                              | Links absent  |
                                                                              +---------------+
```

After **each** edge, preservation requires the same baseline bytes. No Go
compiler for Patronus is needed; a manager's own host/bootstrap prerequisites
remain separate. Linuxbrew prerequisites do not become a compiler-free-host
promise. Rejected architectures/unavailable clients are recorded explicitly.

Use [`evidence-template.json`](evidence-template.json) as a blank observation
record, never a pre-filled pass. Attach actual client/OS/architecture identity,
commands, exits and logs; mark only observed rows passed. For WinGet, add browser
comparison/PR URLs and verified exact-version default-source observation. A
merged PR or staged manifest is not source availability. The secret-free source job
must execute and its authenticated current-run artifact be checked before claiming
controller source observation; no pasted evidence override is supported.
