# Native executable-package checks and lifecycle

Native install, upgrade and uninstall checks run in CI, on disposable
GitHub-hosted runners, through `.github/workflows/native-checks.yml`. This
directory holds the inert preservation fixtures those checks seed and compare,
and a blank evidence template for observations CI cannot make. It concerns
executable packages (Brew/Scoop/WinGet), not Patronus content packages. No check
invokes Patronus content install/update/remove commands.

Status: the fixture-mode matrix, the one-time lifecycle, both negatives and the
second-app matrix passed in tap run
[44079561564](https://github.com/darkquasar/homebrew-tap/actions/runs/44079561564)
with synthetic binaries. Checks of a real release, and any live default-source
WinGet lifecycle, remain **pending**. The workflow internals, harness scripts
and failure prefixes are described in
[`distribution/native/README.md`](../../native/README.md).

## Two kinds of check

| Check | Requirement | When it runs | What it proves |
|---|---|---|---|
| Per-release native check | PD-17 | Every release dispatch (release mode) and every tap PR that touches `distribution/**` or `.github/workflows/**` (fixture mode) | Each channel's generated definitions validate, install from a staging catalogue, report the expected `--version` and uninstall cleanly on every hosted platform |
| Lifecycle acceptance | PD-13 | Fixture mode, only with the PR label `native-lifecycle` | Upgrade from an older version, uninstall and preservation of user state, once per manager and package shape |

Neither check is publication. A WinGet install from local manifests is evidence
for the definitions only, not default-source availability.

```text
+------------------+  => render, stage   +--------------------+  => install, version,
| native-checks.yml| ==================> | hosted runners x6  |     uninstall
+------------------+                     +--------------------+
         |  ~> label native-lifecycle               | ~> reports
         v                                          v
+------------------+  ~> lifecycle.json  +--------------------+
| lifecycle jobs   | ~~~~~~~~~~~~~~~~~~> | native-checks.json |
| [macOS, Windows] |   (own artifacts)   | [summary artifact] |
+------------------+                     +--------------------+
```

## Per-release matrix

| Manager | Platform | Runner label | Steps |
|---|---|---|---|
| Homebrew | macOS arm64 | `macos-15` | `ruby -c` and `brew style`, pour the bottle, `--version`, `brew test`, uninstall |
| Homebrew | macOS Intel | `macos-15-intel` | Same |
| Homebrew | Linux x86_64 | `ubuntu-24.04` | Same |
| Homebrew | Linux arm64 | `ubuntu-24.04-arm` | Same |
| Scoop, WinGet | Windows x64 | `windows-2025` | Schema validation (`winget validate` too), install as an ordinary user, `--version`, uninstall |
| Scoop, WinGet | Windows Arm64 | `windows-11-arm` | Same, natively on Arm64 |

The Homebrew check requires a pour of the expected bottle
(`poured_from_bottle: true`) and no source build. The Windows checks run as a
separate normal user without administrator or symlink privilege and with
Developer Mode off. An executable runs only after its SHA-256 equals the host
architecture's snapshot asset, and `--version` must report `v<version>` as a
whole token. Runner labels live in `distribution/native/pins.json`; a platform
listed there as statically mapped is recorded `statically-mapped`
(`no-hosted-runner`), never as tested. Currently all six labels run.

In fixture mode, `render` builds a synthetic program for all six platforms at
versions 2.7.0 and 2.7.1, freezes a fixture snapshot and serves the assets on
loopback. In release mode it stages the frozen record with the real release URLs
and bottles. Results land in the run's `<label>-checks` artifact as
`native-checks.json` (`native-checks` for the default label), with per-platform
artifacts `<label>-bottle-<platform>`, `<label>-brew-check-<platform>` and
`<label>-windows-<platform>`. Artifacts are kept for 30 days.

## PR labels

Tap pull requests that change `distribution/**` or `.github/workflows/**` run
the fixture matrix for Patronus and the `windows-harness-regression` job. Adding
a label starts a run with more checks:

| Label | Adds |
|---|---|
| `native-lifecycle` | The PD-13 lifecycle jobs `brew-lifecycle` (`macos-15`) and `windows-lifecycle` (`windows-2025`) |
| `native-negative` | Two AC-17 negatives: `expect_failure: arch` swaps the amd64 and arm64 mappings in the staging copies, and `expect_failure: version` serves 2.7.0-stamped binaries as 2.7.1. Every platform must fail with `architecture-mismatch:` or `version-mismatch:` and every channel must be stopped |
| `native-second-app` | The same matrix for the second fixture app (Lumen), with Patronus's staged entries sharing the tap and bucket; they must stay byte-identical and uninstalled |

Lifecycle and negatives run for Patronus only.

## Lifecycle acceptance (PD-13)

Run it once per manager and package shape. Repeat it only when a package shape
or install mechanism changes, for example formula to cask, bottled to
unbottled, or portable to installer-based. Each run records the shape it
covers:

| Manager | Host | Package shape recorded |
|---|---|---|
| Homebrew | macOS arm64, `macos-15` | Homebrew formula, bottled (cellar `any_skip_relocation`), staging tap `darkquasar/tap` (`--no-git`), bottles poured from a loopback `root_url` |
| Scoop | Windows x64, `windows-2025`, normal user | Scoop portable executable from a bucket manifest (shim), local Git bucket, pinned Scoop core held |
| WinGet | Windows x64, `windows-2025`, normal user | WinGet portable installer type from local multi-file manifests, default user scope |

Homebrew on Linux uses the same formula and is covered by the per-release check.
Each lifecycle seeds the preservation sentinels, installs 2.7.0, advances the
staging catalogue to 2.7.1, upgrades, then uninstalls. After every stage it
checks the resolved executable's path, SHA-256 and `--version`, and requires
every sentinel to be byte-identical:

| Manager | Install | Advance and upgrade | Uninstall |
|---|---|---|---|
| Homebrew | `brew install darkquasar/tap/patronus` | Replace the no-git tap's formula with the 2.7.1 staging copy, then `brew upgrade darkquasar/tap/patronus` (must pour, no source build) | `brew uninstall darkquasar/tap/patronus` |
| Scoop | `scoop install darkquasar/patronus` | Commit the 2.7.1 manifest to the local bucket, `git pull` in the registered bucket, then `scoop update patronus` | `scoop uninstall patronus` |
| WinGet | `winget install --manifest <2.7.0 dir>` | `winget upgrade --manifest <2.7.1 dir>` | `winget uninstall --manifest <2.7.1 dir>` |

Two staging adaptations differ from the public consumer commands:

- **Scoop:** the harness advances the registered staging bucket with `git pull`
  and then runs `scoop update patronus`. Bare `scoop update` in the pinned Scoop
  would try to fetch the mutable upstream Main bucket, which the staging host
  does not have. `scoop config hold_update_until` keeps the pinned Scoop core.
- **WinGet:** a local-manifest installation is removed with
  `winget uninstall --manifest`, because `winget uninstall --id
  darkquasar.Patronus --exact` does not find it. This says nothing about
  uninstalling a default-source installation.

`brew update` is not run, because the staging tap has no Git history. Evidence
is in the `native-brew-lifecycle` and `native-windows-lifecycle` artifacts
(`lifecycle.json`, `lifecycle-scoop.json`, `lifecycle-winget.json`, the
per-stage `preservation-*.json` and the manager logs).

## Preservation sentinels

`prepare.py preservation seed` copies the files listed in
[`preservation/manifest.json`](preservation/manifest.json) into a disposable
project and the runner user's home, refusing to overwrite any existing file.
`prepare.py preservation check` then compares the seeded tree after each stage.
The Windows harness does the same for the separate normal user. The sentinels
contain invented data, no keys, hooks or programs. State records have their
matching scope (`local`/`global`) and a checksum tied to the invented skill;
they are preservation data, never an approval or native result.

| Sentinel | Project-relative target | Home-relative target |
|---|---|---|
| Patronus state | `.patronus/state.json` | `.patronus/state.json` |
| Claude settings / instructions | `.claude/settings.json`, `CLAUDE.md` | `.claude/settings.json`, `.claude/CLAUDE.md` |
| Codex configuration / instructions | `.codex/config.toml`, `AGENTS.md` | `.codex/config.toml`, `.codex/AGENTS.md` |
| OpenCode configuration / instructions | `opencode.json`, `AGENTS.md` | `.config/opencode/opencode.json`, `.config/opencode/AGENTS.md` |
| Pi configuration / instructions | `.pi/settings.json`, `AGENTS.md` | `.pi/agent/settings.json`, `.pi/agent/AGENTS.md` |
| Managed content / user content | `.claude/skills/invented-preservation/SKILL.md`, `content-package/data.txt` | Same relative paths |
| Unrelated bytes | `unrelated.txt` | `unrelated.txt` |

Paths match the accepted adapters' default roots. Manager-owned paths and
caches are expected to change and are not preservation targets.

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
This local staging is structural only. Do not publish Lumen or open its
upstream PR. Fixture URLs are invented inputs; do not download from them as if
they were real release binaries. Native installation of both apps, with
synthetic binaries and Patronus's coexisting entries, is the CI
`native-second-app` run above.

## Live default-source lifecycle (pending)

CI cannot observe Microsoft's default WinGet source before a version is indexed,
and local-manifest identity can differ from it. A live-source lifecycle needs
both an older and a newer version indexed, then
`winget install --id darkquasar.Patronus --exact --source winget`,
`winget upgrade --id darkquasar.Patronus --exact --source winget` and
`winget uninstall --id darkquasar.Patronus --exact` on a disposable Windows
normal-user host, under a separate native grant. Until then that row stays
pending. The earlier synthetic Windows x64 run
[42178401448](https://github.com/darkquasar/patronus/actions/runs/42178401448)
and its [procedure](https://github.com/darkquasar/patronus/blob/main/tools/tests/package-windows-validation.md)
remain historical T3 evidence only.

Use [`evidence-template.json`](evidence-template.json) as a blank observation
record for such manual rows, never a pre-filled pass. Attach actual
client/OS/architecture identity, commands, exits and logs; mark only observed
rows passed. For WinGet, add browser comparison/PR URLs and verified
exact-version default-source observation. A merged PR or staged manifest is not
source availability. The secret-free source job must execute and its
authenticated current-run artifact be checked before claiming controller source
observation; no pasted evidence override is supported.
