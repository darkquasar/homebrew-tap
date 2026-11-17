# Native package checks

`.github/workflows/native-checks.yml` runs the per-release native checks (PD-17),
builds Homebrew bottles (PD-16) and, on request, the one-time lifecycle acceptance
(PD-13). Every job runs on a GitHub-hosted runner with `contents: read`, no
environment and no secrets. Only the existing `distribute.yml` publisher writes.

```text
  +--------+   artifact    +--------+  fresh  +------------+       +----------+
  | render | ~~~~~~~~~~~~> | select | ======> | brew-build | ====> | assemble |
  +--------+               +--------+         | x4 hosts   |       +----------+
      |                         | published   +------------+            |
      | inputs                  +===================================>    |
      v                                                                  v
  +---------+  reports   +---------+   native-checks.json   +------------------+
  | windows | ~~~~~~~~~> | summary | <~~~~~~~~~~~~~~~~~~~~~ | brew-check x4    |
  | x2      |            +---------+                        +------------------+
  +---------+                 | => artifact native-checks (+ native-brew-candidate)
                              v
                      +------------------------+
                      | distribute.yml publish |
                      +------------------------+
```

## Modes

- `release` (called by `distribute.yml` after `authority`): `render` loads the
  registration at the control commit and the snapshot from the records branch at
  `record_ref.records_commit`, checks the locator, and stages candidates whose
  staging copies equal the canonical bytes, so managers download the real release
  URLs. `select` reads the public tap release `<app>-<version>`: a complete
  published set is reused, none means a fresh build, anything else is a conflict
  and `brew` is recorded as `null`.
- `fixture` (called by `ci.yml` on pull requests that change `distribution/**` or
  `.github/workflows/**`): `render` builds the synthetic program in
  `fixture/main.go` once for all six platforms and versions 2.7.0 and 2.7.1,
  freezes a fixture snapshot, and serves the assets on loopback. Label
  `native-lifecycle` adds the lifecycle jobs; label `native-negative` adds the two
  AC-17 negatives (`expect_failure: arch` and `version`).

## Files

- `prepare.py`: canonical candidates (exactly `validation.render`), loopback
  staging copies (Scoop `url`, WinGet `InstallerUrl`, formula `url` lines; the
  bottled formula only through `bottles.staging_formula`), bottle assembly,
  platform reports, `native-checks.json` (schema v1, validated with
  `bottles.validate_native_checks`), negative verdicts and the preservation
  sentinel inventory. Every output tree records the SHA-256 of each file.
  Tests: `distribution/tests/test_native_prepare.py`.
- `brew_check.sh`: Homebrew host setup (the image's Homebrew, or the pinned
  installer on Linux), bottle builds from a `--no-git` staging tap named
  `darkquasar/tap`, the pour check and the macOS lifecycle.
- `windows_check.ps1` and `windows_user.ps1`: Scoop and WinGet as a separate
  normal user (no administrator or symlink privilege, Developer Mode off,
  RemoteSigned), for the host architecture.
- `pins.json`: runner labels, statically mapped platforms, loopback roots, the
  Homebrew installer pin and the Windows client downloads.

## Diagnostics

A failing check reports one reason prefix: `architecture-mismatch:` (the resolved
executable is not the host architecture's snapshot asset), `version-mismatch:`
(`--version` lacks `v<version>`), or `validation-failed:`, `install-failed:`,
`uninstall-failed:`, `bottle-build-failed:`, `harness-failed:`. A missing report
fails its platform. A platform in `statically_mapped` without a report is
recorded `statically-mapped`, never as tested.

## Provenance

`windows_check.ps1`, `windows_user.ps1`, the Windows part of `pins.json` and the
fixture staging in `prepare.py` are reviewed ports of Patronus
`tools/tests/package-windows-{check.ps1,user.ps1,pins.json,prepare.py}` at commit
`a532c29203eb716d566cc622f0331525be1ee074` (validated by Patronus run 42178401448).
The Patronus copies stay unchanged; producer code never runs in the controller.
The Homebrew installer pin comes from Patronus `tools/vms/bootstrap-ubuntu.sh`
at the same commit.
