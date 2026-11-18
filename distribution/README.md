# Distribution contracts (schema v1)

This directory belongs on `distribution-control` in `darkquasar/homebrew-tap`.
Sections named after a milestone (T1, T3, T4) are historical: they record what
that milestone delivered and left for later. The current Homebrew bottle and
per-release native-check contracts (T6, T7) are in
[Bottles and native evidence](#bottles-and-native-evidence).
Historically, T1 supplied pure contracts and read-only trust checks. It does not deploy a
workflow, create credentials, write a record or publish a package. The production
GitHub adapter, CLI, CAS persistence and isolated jobs belong to T4. All fixtures
are invented; their App IDs and approval references must never be registered live.

```text
+------------------+  => fresh admission  +-----------------+
| Protected config | ==================> | Authority job   |
+------------------+                     +-----------------+
                                                   |
                                   => inspected identity + owner/run proof
                                                   v
+------------------+  => verified reads  +-----------------+
| Protected record | ==================> | Publisher job   |
+------------------+                     +-----------------+
```

## Public Python interfaces and trust preconditions

All invalid inputs fail closed. Contract rejection raises `ValueError`; unavailable
external evidence, malformed archives or missing files may propagate their native
exception. Callers must treat every exception as denial, never fall back to status
cache or a supplied approval. These modules contain no token minting, subprocess,
network client or write primitive.

| Function | Result / preconditions |
|---|---|
| `contracts.canonical_digest(document)` | SHA-256 of UTF-8 JSON, sorted keys, compact separators, `ensure_ascii=False`. Reject floats, NaN, non-string keys and non-JSON types. Hash exactly the explicit argument; drop no fields. |
| `contracts.validate_registration(document)` | Deep copy of the strict v1 envelope after digest, owner, scope and shape checks. Structural validation is **not authentication**. Use only the protected registry after admission, or an owner-admitted onboarding candidate. |
| `trust.authorize_controller(registration, context, github)` | Admission evidence bound to fresh control SHA, mode, role, config digest, run/attempt and both actors. Reads independent run and protected registry evidence. No secrets accessed. |
| `trust.verify_release(registration, receipt, github)` | A snapshot candidate after independent artifact, workflow, run and downloaded-byte verification. Authority job must first admit the requesting bot/owner with `authorize_controller`; a valid source release alone does not authenticate its caller. |
| `snapshot.inspect_identity(registration, tag, github)` | Current downloaded identity after repository ownership, recursive tag peeling, main ancestry, stable-release and six checksum checks. This is an inspection, not approval. |
| `snapshot.approve_snapshot(registration, snapshot, approval)` | Baseline snapshot candidate. Here `snapshot` is the freshly inspected identity dictionary. The authority job constructs approval from its authenticated owner admission plus the explicitly approved digest, never accepts an approval dictionary from dispatch input. It must freshly inspect in that same operation and reject changed bytes. This pure helper has no independent authentication channel. |
| `snapshot.load_authorized_snapshot(registration, record_ref, github)` | Deep copy of the original protected snapshot, only for a freshly admitted publisher, after protected-history/read-back/current-byte checks. Includes channels with no prior destination state. No record reconstruction from current metadata. |
| `snapshot.validate_snapshot(registration, document)` | Strict structural/digest check for an already trusted document; it does not authenticate arbitrary caller-supplied JSON. |

`context` has exactly `repository`, `ref`, `sha`, positive integer `run_id` and
`attempt`, `mode`, `role`, and `app`. Repository/ref/SHA/run/attempt come from the
current workflow environment, not the dispatch payload. `mode` and `app` are
untrusted request selectors, checked here; role is fixed by the authority/publish
job, never selected by the producer. Modes are `prepare`, `register`,
`inspect-baseline`, `approve-baseline`, `retry`, `status`; roles are `authority`
and `publish`. Register cannot publish. Owner modes require original and triggering
actor ID 28435534. Prepare requires both registered bot IDs and the registered
App-to-bot association. All modes require the fixed tap repository, branch ref
`refs/heads/distribution-control`, fresh branch tip and registered workflow path.

A register candidate is not yet in the registry. Owner admission permits checking
it against all *other* apps for identity/destination collisions; T4 emits the
approved envelope for the owner to commit. Automation does not write control.

## JSON documents

The two `*-registration.json` fixtures give complete field/type examples. A v1
registration has exactly `schema_version: 1`, `configuration`,
`configuration_digest`, `approval`. The digest covers configuration alone.
Approval contains `owner_id`, nonempty authenticated `reference`, and the same
`configuration_digest`. A reference is retained provenance, not a signature;
protected branch origin is indispensable. Any configuration edit needs new owner
approval. Changing approval reference never changes the configuration digest.

Configuration fields are explicit: app, owner login/ID, source repository,
history branch, fixed stable tag policy, dedicated dispatcher App/bot IDs,
workflow path, homepage/description/license, native identifiers, six-asset map,
and three fixed catalogue repo/path entries. Unknown fields fail. Native brew,
scoop and command names equal the app slug. The WinGet ID is `darkquasar.Name`,
with case-folded Name equal to the app slug without hyphens. Derived paths are
`Formula/<app>.rb`, `bucket/<app>.json`, and `manifests/d/darkquasar/Name`.
Whole-registry admission rejects path overlap and shared dispatcher IDs. Names
are compared case-insensitively for destination collisions. Traversal cannot be
expressed by these contracts; the adapter additionally rejects symlink/tree entries.
Patronus's source repository and six existing asset names are fixed.

The receipt in `patronus-receipt.json` records repository/tag/version/peeled commit,
positive run/attempt, registered workflow path, observed workflow revision/hash,
push event and full tag ref, both owner IDs, checksum-file digest and exact
`{asset_name: {size: integer, sha256: lowercase_hex}}` map. It is read from
`receipt.json`, the sole member of the immutable artifact
`distribution-receipt-<run>-<attempt>`. Download/archive digests and API association
must match. No extraction or execution occurs. Receipt archive and uncompressed
JSON have a 1 MiB bound. Required binaries must be nonempty; T4's download adapter
must enforce streaming size/time limits before buffering.

Snapshot envelope: `schema_version`, `payload`, `snapshot_digest`. Payload:

- `identity`: app, repository, tag, version, peeled commit, `checksums_sha256`,
  exact asset name/size/hash map. `identity_digest` hashes this dictionary alone.
- `authorization`: `kind` (`release-run` or `baseline`), `reference`, `owner_id`,
  `identity_digest`. Release reference records source run/attempt, artifact ID/hash
  and workflow path/revision/audit hash. Baseline reference is the authenticated
  owner's approval reference and never fabricates a historical push event.
- `initial_configuration_digest`: original owner-approved configuration for audit.

`patronus-snapshot.json` demonstrates release-run authority; `lumen-snapshot.json`
demonstrates an independently versioned baseline. `snapshot_digest` hashes payload,
not itself. The persistence layer must create once, compare on races and reuse the
original record. These functions do not permit writing/overwriting any record.

Baseline approval input has exactly `owner_id`, `triggering_actor_id`,
`mode: approve-baseline`, `role: authority`, admitted `controller_sha`,
`configuration_digest`, explicitly approved `identity_digest`, and authenticated
`reference`. The admitted job constructs these fields; JSON shape alone grants
no authority.

`record_ref` has exactly `schema_version: 1`, `app`, `version`, `controller_sha`,
`configuration_digest`, `records_commit`, `snapshot_digest`. It contains no path,
command, token or role. Publisher compares both the named reachable records commit
and current records tip at the derived `.distribution/releases/<app>/<version>.json`.
It then loads the original approved configuration from protected control history
and compares all scope fields. Homepage, description, license and owner-approved dispatcher replacement may differ
on an old snapshot. Changed source, native identity, destinations or other authority
fields cannot reinterpret that snapshot. A compatible new release/owner decision
is required when scope changes. Current configuration digest belongs in T4 channel
results; it never replaces the snapshot's original configuration/authorization.

Channel results are T4-owned: app/version/channel, state, snapshot/current config/
definition digests, validation evidence, destination commit, durable branch/compare/
PR refs, observation time, failure code and actionable message. States are
`generated`, `validated`, `prepared`, `submitted`, `accepted-awaiting-indexing`,
`published`, `failed`, `rejected`, `unknown`, `skipped`. T1 does not claim these
operations or native validation have run.

## External read adapter contract for T4

Methods below describe the required real external observations. They must not
return caller-supplied facts as if independently verified. Missing/ambiguous reads
raise; pagination must be complete; no error becomes an empty success. T1 tests
use an invented external service. T4 must test the same contract against its real
API translation before integration. No T1 mock replaces an owned module.

| Method | Return / required evidence |
|---|---|
| `repository(repo)` | `full_name`, numeric `owner_id`, from the repository API. |
| `ref(repo, ref)` | `{type, sha}` Git object for `heads/...` or `tags/...`; no fabricated fallback. |
| `tag(repo, sha)` | Annotated tag target `{type, sha}`; caller peels at most 16, rejecting cycles. |
| `is_ancestor(repo, ancestor, tip)` | Exact boolean from independent Git/compare evidence, true only on proven ancestry. |
| `run(repo, id, attempt)` | Exact attempt's `id`, `attempt`, `repository`, `workflow_path`, `head_sha`, `head_branch`, `event`, `actor_id`, `triggering_actor_id`, plus source run `status`/`conclusion`. Map API `run_attempt`, `path`, nested repository/actors without trusting workflow display name or the dispatch payload. |
| `user(actor_id)` | `GET /user/{account_id}` returns the actual numeric ID, `type: Bot` and `<slug>[bot]` login. |
| `app(slug)` | `GET /apps/{slug}` returns matching App `id` and `slug`; compare to the owner-approved dispatcher App ID. Neither lookup requires the dispatcher private key. |
| `list_registrations(repo, control_sha)` | Complete registration envelopes at that exact protected control commit. Read only `.distribution/registrations/*.json`, reject symlinks, duplicate JSON keys and non-file entries. Empty is valid only for a proven empty registry during first owner registration. |
| `file(repo, commit, path)` | `{type: file/symlink/..., content: bytes}` from an immutable Git blob, preserving actual entry type. Never dereference a symlink into trusted evidence. |
| `release(repo, tag)` | Actual published `tag_name`, booleans `draft`/`prerelease`, fully paginated `assets: [{name, size}]`. Ancillary assets are allowed, but required checksums must enumerate exactly the registered binaries. |
| `download_asset(repo, tag, name)` | Downloaded bytes from derived approved GitHub release coordinates. Reject arbitrary URL inputs; only GitHub release-asset delivery redirects, bounded request time/read retries/size. |
| `receipt_artifact(repo, id, attempt)` | API-verified artifact `{id, name, run_id, head_sha, expired, sha256, content}`. Match API workflow_run.id/head_sha and the unique attempt-qualified artifact name. Attempt association comes from that name plus receipt contents, not an API attempt field. Require a non-null sha256: API digest and verify the actual downloaded ZIP. No metadata supplied by producer dispatch. |
| `controller_context()` | Context from this job's real environment plus bounded request selectors; role is fixed by job. Used for independent publisher re-admission. |
| `registration_revision(app, digest, current_control_sha)` | `{commit, registration}` from actual protected control history, with digest match; caller verifies commit ancestry. Never search the publisher-writable default branch. |

GitHub run responses expose `head_branch`, not a fully qualified ref. For source
runs, it must match the stable tag; the independently downloaded, trusted
workflow-produced receipt binds `refs/tags/<tag>`. For controller runs, the actual
job environment supplies the fully qualified branch ref and the API head name
must match. No adapter may invent a full ref by prefixing a requested tag.
The [workflow-run attempt API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt)
and a read-only observation of Patronus run 40148694836 informed this contract;
that observation is not approval of the old release or evidence of T2 dispatch.

## Tests and remaining integration (historical, T1)

Run `python3 -B -m unittest discover -s distribution/tests -p 'test_*.py'`.
The paragraphs below are the T1 handoff as written. T1 uses public-function test-first slices for ownership, authority and identity,
plus negative/legacy regressions. It reads no credentials and performs no live
release download, publication, binary execution or native manager operation.

T4 must enforce native rules/environment protection, trusted job inputs, fresh
inspection at baseline approval, create-only records/CAS, role-specific token
lifetime, safe downloads, and independent isolated job execution. T2 must bind
receipt upload to the real successful attempt and preserve non-fatal receipt-only
failure policy. At T1, native/ARM/package checks belonged to later milestones;
T6 and T7 have since delivered them (see
[Bottles and native evidence](#bottles-and-native-evidence)). Those
requirements are not established by T1's invented API evidence.

## Bottles and native evidence

T6 added Homebrew bottles and the publisher's native gate; T7 added the
unprivileged `native-checks.yml` workflow that produces the evidence. Workflow
jobs, harness scripts and failure prefixes are described in
[`native/README.md`](native/README.md); the CI procedure is in
[`tests/native/README.md`](tests/native/README.md).

### `bottles.py`

Every function takes a validated registration and snapshot and fails closed.
The first two exception classes separate structure from identity:

| Class | Meaning |
|---|---|
| `BottleInvalid(ValueError)` | Structure: members, links, size, tag form, root URL or bottle_set shape. |
| `BottleConflict(ValueError)` | Integrity: a bottled executable differs from the frozen snapshot asset (including another platform's asset), or a published release does not match. Never repaired by replacement. |
| `NativeEvidenceInvalid(ValueError)` | `native-checks.json` fails schema v1. |

| Function | Result / preconditions |
|---|---|
| `verify_bottle(archive, registration, snapshot, tag)` | Reads one archive (1 byte to `MAX_ARCHIVE`, 256 MiB, uncompressed at most `MAX_UNCOMPRESSED`) without extracting or running it. Allows only `<formula>/<version>/bin/<command>` plus optional `INSTALL_RECEIPT.json`, `.brew/<formula>.rb` and `sbom.spdx.json` and their parent directories. Rejects links, sparse, special or duplicate members, unsafe paths and trailing gzip data (`BottleInvalid`). Requires the executable's SHA-256 to equal the snapshot asset for the tag's platform (`BottleConflict`). Returns `tag`, `sha256`, `executable_sha256`, `asset` (remote filename) and `members`. |
| `bottle_tag_platform(tag)` | `(system, cpu)` for `x86_64_linux`, `arm64_linux` or `arm64_<pinned macOS name>`; else `BottleInvalid` (Intel macOS is not bottled). |
| `remote_filename(formula, version, tag)` | `<formula>-<version>.<tag>.bottle.tar.gz`, URL-encoded as Homebrew does for rebuild 0. |
| `release_tag(configuration, identity)` / `canonical_root_url(...)` | `<app>-<version>` and `https://github.com/<tap>/releases/download/<app>-<version>`, the only tag and root URL an app may use. |
| `bottle_set(registration, snapshot, verified, root_url)` | Assembles `verify_bottle` results into `{release_tag, root_url, cellar, tags}`, then `check_bottle_set`. |
| `check_bottle_set(registration, snapshot, bottles)` | Exact fields, the app's own tag and canonical root URL, cellar `any_skip_relocation`, and exactly one tag per Brew platform (`darwin-arm64`, `linux-amd64`, `linux-arm64`). |
| `bottle_block(...)` / `ordered_tags(tags)` | The formula's `bottle do` block in Homebrew's tag order and alignment. `validation.render('brew', ..., bottles)` inserts it; `validation.validate_candidate(..., bottles)` requires exactly that block and reports `bottle_set_sha256`. |
| `bottled_formula_identity(registration, snapshot, bottles)` | `formula_sha256`, `bottle_set_sha256`, `snapshot_digest`, `configuration_digest`: the identity native checks bind to and the publisher reproduces. |
| `staging_formula(canonical, loopback_root_url)` | Replaces only the `root_url` line with a loopback URL for staging installs; any other change raises `ValueError`. |
| `select_bottles(registration, snapshot, github)` | Unprivileged read of tap release `<app>-<version>`: `None` when unpublished; a complete release whose three assets verify is selected as is; anything else published is `BottleConflict`. |
| `validate_native_checks(document)` | Strict schema v1 check (below); returns a copy. |
| `native_failures(channel, registration, snapshot, evidence, binding, context, bottles)` | The failing platforms or evidence problems that stop one channel; an empty list passes the gate. |

### `verify-bottle` (offline, credential-free)

```sh
python3 -B -m distribution.cli verify-bottle --archive FILE --tag TAG \
  --registration REGISTRATION.json --snapshot SNAPSHOT.json --out RESULT.json
```

The result JSON goes to `--out` and standard output. Exit 0: verified. Exit 1:
`InputInvalid`, the registration or snapshot cannot be read or is invalid.
Exit 2: `BottleInvalid`, including an unreadable archive or unsupported tag.
Exit 3: `BottleConflict`. Error JSON is `{"error": ..., "message": ...}` and
names the member or digest. The `brew-build` jobs fail on any nonzero exit.

### `native-checks.json` schema v1

The `summary` job writes it and the publisher reads it. Unknown or missing
fields are rejected at every level:

- `schema_version` 1, `app`, `version`, `snapshot_digest`,
  `configuration_digest`.
- `candidates`: `scoop` and `winget` digests (`state.file_digest` of the
  re-rendered candidate).
- `brew`: `null` when no bottle set was selected or assembled, otherwise
  `source` (`published` or `fresh`), `bottle_set` and `identity` (the
  `bottled_formula_identity` fields).
- `results`: at most 32 entries, one per manager and platform, each with
  `manager`, `platform`, `outcome` (`passed`, `failed` or `statically-mapped`),
  `reason`, `runner`, `image`, `image_version`, `manager_version`, `steps`
  (`validate`, `install`, `version`, `uninstall`: `passed`, `failed`, `skipped`
  or `not-run`) and `binding`. `passed` needs every step passed;
  `statically-mapped` needs every step `not-run`.
- `run`: `id`, `attempt` and
  `url` = `https://github.com/darkquasar/homebrew-tap/actions/runs/<id>/attempts/<attempt>`.

`native_failures` then checks the bindings:

- App, version, snapshot digest and configuration digest equal the publisher's.
- **Same run:** `run.id` equals the publisher's own workflow run and
  `run.attempt` is at most its current attempt, so evidence from another run
  cannot be replayed.
- **Brew:** `brew` is not `null`, its `bottle_set` equals the set being
  published, its `identity` equals the recomputed `bottled_formula_identity`,
  and all three Brew platforms passed with `binding` equal to that
  `formula_sha256`.
- **Scoop and WinGet:** `candidates[channel]` equals the re-rendered digest,
  `windows-amd64` passed, and `windows-arm64` passed or is `statically-mapped`
  with reason `no-hosted-runner` (the only accepted static mapping), each bound
  to that digest.
- A missing result fails its platform.

An example that passes all three gates is
`tests/fixtures/bottles/native-checks.example.json`.

### Publisher inputs and Brew's bottle requirement

The publish job downloads its own run's `native-checks` and
`native-brew-candidate` artifacts and sets:

| Variable | Contents |
|---|---|
| `DISTRIBUTION_NATIVE_CHECKS` | Path to `native-checks.json`. Unset or unreadable means missing evidence: every channel fails closed with `native-check-failed`, and the result records `native_input_errors`. |
| `DISTRIBUTION_BOTTLES_DIR` | Directory of regular files named `<app>-<version>.<tag>.bottle.tar.gz`, at most 8, each at most 256 MiB, no symlinks. Other names are ignored. A missing or differing archive fails Brew with `native-check-failed`. |

`controller.publish(registration, record_ref, github, credentials, channels,
*, evidence=None, archives=None)` treats both as untrusted and re-derives every
binding. A channel whose gate fails returns `failed: native-check-failed` with
`failing_platforms`; others proceed. Each channel result carries
`native_checks` (`run` and canonical `digest` of the evidence). For Brew,
`apply_bottles` re-verifies every archive, reuses a published set or publishes
its own draft release, reads the assets back from their public URLs and only
then returns the bottle set (failures: `native-check-failed`, `bottle-invalid`,
`bottle-conflict`, `bottle-race`, `destination-unavailable`). `apply_channel('brew', ...)` without
a bottle set returns `failed: bottles-required`: an unbottled formula is never
published. The Brew marker and result gain `release_tag`, `bottle_set_sha256`
and `bottles` (tag to SHA-256). Recovery for each code is in
[`docs/recovery.md`](../docs/recovery.md#bottle-and-native-check-failures).

## T3 native candidate preflight

Registration approval validates the T1 structure and provenance; it does not
guarantee native package acceptance. Before requesting approval of presentation
metadata, render and validate a candidate for every intended channel. Homebrew
currently accepts descriptions of at most 80 characters, starting with a capital
letter (with its documented product-name exceptions), without a leading article,
the formula name, surrounding spaces or a trailing full stop. WinGet's vendored
1.10 schema requires `ShortDescription` to contain 3–256 characters. The real
native tools and vendored schemas remain authoritative. Do not truncate or
rewrite approved metadata silently; correct the registration and obtain owner
approval before rendering again. The native-registration fixtures demonstrate
this separate presentation approval while retaining the frozen release identity.

Install the hash-locked `validation-requirements.txt` in a Python 3.12–3.14
environment. The maintainer entry point is:

```sh
python -B -m distribution.validation --channel scoop --app patronus \
  --registration registration.json --snapshot snapshot.json \
  --candidate-dir staging --out validation.json
```

Use `brew` or `winget` for the other channels. Stage the exact path/byte mapping
returned by `validation.render`; all channels reject edits or alternate encodings
of those bytes, even when semantically equivalent. A successful result binds the
candidate, snapshot and configuration digests. Every result explicitly records
`authority: not-evaluated`: these local structural checks never authorize a
release. T4 must admit the caller and load the protected authorized snapshot
through T1 before validation and publication, and must reject changed bytes.

Homebrew validation first warms its toolchain against a separate syntax probe
with a five-minute bound, then checks each candidate with Homebrew's own Ruby and
style checker, each bounded at 60 seconds. Toolchain/information failures produce
`validation-unavailable`; native candidate errors produce `validation-failed`.
The subprocess environment forwards only named runtime/cache settings, without
GitHub tokens, App keys or Ruby/Bundler injection settings. T4 must bootstrap
tools before minting publication credentials and preserve the workflow's overall
job timeout. T4/T5 onboarding must run this preflight before registering metadata;
the CLI here does not implement that future credentialed workflow.

```text
+-------------------+  => render bytes  +------------------+  => native/schema  +-------------+
| Approved metadata | ===============> | Staged candidate | =================> | Byte proof  |
+-------------------+                  +------------------+                    +-------------+
```
