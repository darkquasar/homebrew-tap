# Distribution contracts (schema v1)

This directory belongs on `distribution-control` in `darkquasar/homebrew-tap`.
T1 supplies pure contracts and read-only trust checks. It does not deploy a
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

## Tests and remaining integration

Run `python3 -B -m unittest discover -s distribution/tests -p 'test_*.py'`.
T1 uses public-function test-first slices for ownership, authority and identity,
plus negative/legacy regressions. It reads no credentials and performs no live
release download, publication, binary execution or native manager operation.

T4 must enforce native rules/environment protection, trusted job inputs, fresh
inspection at baseline approval, create-only records/CAS, role-specific token
lifetime, safe downloads, and independent isolated job execution. T2 must bind
receipt upload to the real successful attempt and preserve non-fatal receipt-only
failure policy. Native/ARM/package checks belong to later milestones. Those
requirements are not established by T1's invented API evidence.
