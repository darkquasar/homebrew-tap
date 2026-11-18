# Register an app and approve its first executable package

**PENDING commissioning.** Use this procedure only after [setup](setup.md),
reviewed deployment and a separate owner operation/publication grant. It creates
executable packages (Brew/Scoop/WinGet), not Patronus content packages. All IDs,
release choices and bytes need actual owner inspection; fixtures grant nothing.

Run local preparation from a clean checkout of accepted tap control code, with
Python 3.12 and the accepted hash-locked validation dependencies. `gh` uses the
owner's normal authentication; never export App private keys into a local shell
just to run these commands. Set `distribution_work` to a new directory outside
checkouts. Placeholder run IDs below come from the exact returned Actions URL,
not “the latest run.” Do not execute production examples without their explicit owner operation grant.

## Inspect configuration and native metadata

Patronus's registration configuration must name `darkquasar/patronus`, owner
`darkquasar` / `28435534`, history `main`, stable `vMAJOR.MINOR.PATCH`,
`.github/workflows/release.yml`, its six fixed raw asset names, real homepage,
license/description, and the actual dedicated dispatcher numeric App/bot IDs.
Discover the bot via `gh api users/APP-SLUG%5Bbot%5D --jq '{id,login,type}'`
and App via `gh api apps/APP-SLUG --jq '{id,slug}'`; verify both rather than
assuming that a client ID or an installation ID is the App ID.

The complete JSON shape is in
[`patronus-native-registration.json`](../distribution/tests/fixtures/patronus-native-registration.json).
Extract its `configuration` only as an editing template:

```sh
distribution_work=$(mktemp -d)
jq '.configuration' distribution/tests/fixtures/patronus-native-registration.json > "$distribution_work/configuration.json"
```

Replace the **invented** dispatcher `9001` / bot `8001` and synthetic description
before requesting real approval. Preserve the existing assets/native identity:

| Channel | Identifier / command | Exact allowed destination |
|---|---|---|
| Brew | `patronus` | `darkquasar/homebrew-tap:Formula/patronus.rb` |
| Scoop | `patronus` | `darkquasar/scoop-bucket:bucket/patronus.json` |
| WinGet | `darkquasar.Patronus`, command `patronus` | `darkquasar/winget-pkgs:manifests/d/darkquasar/Patronus/<version>/` |

Check presentation metadata before approval: Homebrew description style/80-char
limit, WinGet ShortDescription 3–256 characters, license and HTTPS homepage.
Use the actual renderers/native/schema preflight described in
[distribution contracts](../distribution/README.md#t3-native-candidate-preflight)
with an invented staging snapshot; structural validation says
`authority: not-evaluated`. Do not silently truncate/rewrite metadata after owner
approval. All three channels must pass their exact-byte candidate checks before
actual distribution. WinGet uses the complete three-file manifest set, official
1.10.0 schema headers and portable manifests without `Scope`; x86 is unsupported.

Compute the digest with the same canonical function as the controller, from the
tap root. This is a configuration digest, **not** SHA-256 of pretty-printed JSON:

```sh
configuration_digest=$(python3 -B - "$distribution_work/configuration.json" <<'PY'
import sys
from pathlib import Path
from distribution.snapshot import strict_json
from distribution.contracts import canonical_digest, validate_configuration
configuration = strict_json(Path(sys.argv[1]).read_bytes())
validate_configuration(configuration)
print(canonical_digest(configuration))
PY
)
```

The owner inspects the complete configuration and explicitly approves this exact
digest. Changing source identity, destinations or authority needs new approval.
The protected whole registry rejects overlapping app names/paths (case-insensitive
for destination collisions), shared dispatcher App/bot identities and unsafe files.

## Owner-only registration: emit, then apply protected bytes

With that explicit owner approval, create the dispatch inputs as JSON data:

```sh
jq -n --arg app patronus --rawfile candidate "$distribution_work/configuration.json" \
  --arg digest "$configuration_digest" \
  '{mode:"register",app:$app,candidate:$candidate,approve_sha:$digest}' \
  > "$distribution_work/register-inputs.json"
gh workflow run distribute.yml --repo darkquasar/homebrew-tap \
  --ref distribution-control --json < "$distribution_work/register-inputs.json"
```

Record the exact registration run ID as `register_run`. Inspect/download its
public result; owner registration emits an envelope and never writes control or
publishes packages:

```sh
gh run view "$register_run" --repo darkquasar/homebrew-tap --json url,event,headSha,status,conclusion
gh run download "$register_run" --repo darkquasar/homebrew-tap \
  --name "distribution-authority-$register_run-1" --dir "$distribution_work/register-result"
```

The `-1` suffix means the original attempt; if a permitted owner-only operation
was rerun, substitute its recorded attempt. The artifact contains
`distribution-authority.json`; on successful register this is the approved
registration envelope itself. Check schema, owner/configuration digest and its
actual authenticated Actions approval reference. A failed-result JSON is not a
registration. Review bytes against the owner's candidate.

In an owner-managed clean control checkout (`control_checkout`, checked branch
`distribution-control` at the recorded current remote tip), apply exactly that
successful envelope:

```sh
mkdir -p "$control_checkout/.distribution/registrations"
cp "$distribution_work/register-result/distribution-authority.json" \
  "$control_checkout/.distribution/registrations/patronus.json"
git -C "$control_checkout" diff -- .distribution/registrations/patronus.json
git -C "$control_checkout" add .distribution/registrations/patronus.json
git -C "$control_checkout" commit -m 'Register owner-approved Patronus executable distribution'
git -C "$control_checkout" push origin HEAD:refs/heads/distribution-control
```

Inspect a new untracked registration's complete bytes too (`git diff` alone omits
it). The owner applies this normal commit; automation has no control-branch
bypass. Record resulting control SHA and envelope hash. Start a **new dispatch**
after application: the registration run names the old control SHA and cannot
be reused. A fixture's approval string or a local `register` call is not protected
authority.

## New releases with intact receipts

The approved Patronus main dispatcher handles a successful owner release through
`workflow_run`, after the binaries/checksums and exact attempt receipt exist.
Only canonical stable `vMAJOR.MINOR.PATCH` tags enter executable catalogues;
drafts, prereleases and suffixes do not. Content-only publication does not enter
this path. Original and rerun source actors must both be owner `28435534`.

For an existing successful release with its intact original receipt, the owner
can request a fresh producer dispatch, without rebuilding:

```sh
gh workflow run package-distribution.yml --repo darkquasar/patronus --ref main \
  -f run_id="$release_run" -f attempt="$release_attempt"
```

The verified producer App dispatches controller `prepare` with exactly app,
source `run_id` and `attempt`, selecting `distribution-control`. An owner must
not directly run controller `prepare` as themselves: that mode requires the
registered bot as both original and triggering actor. Authority independently
reads source run/attempt and receipt using the separate read-only producer App,
verifies all six current assets/checksums, creates/reuses the protected snapshot
through CAS, reads it back and hands only `record_ref` to publisher.

Receipt capture/upload failure is non-fatal to normal binary publication; direct
downloads remain usable. Without the receipt and without an existing protected
snapshot, use the explicit baseline procedure below. Never rebuild/retag merely
to recover packaging evidence. With an existing protected record, use owner
`retry` for the selected channel instead.

## Historical release or missing receipt: inspect and approve a baseline

The plan selected `v2.7.1` for inspection at peeled commit
`f8587969586b37aef1aebe9a79ac03c6b08f5f30`; recheck that identity and ancestry.
That selection does not approve its bytes.
The owner may select another eligible published stable release explicitly. Start
with inspection only:

```sh
gh workflow run distribute.yml --repo darkquasar/homebrew-tap --ref distribution-control \
  -f mode=inspect-baseline -f app=patronus -f tag=v2.7.1
```

Record `inspect_run` and its exact attempt (`inspect_attempt`, initially `1`).
Download its authority artifact to a fresh directory:

```sh
gh run download "$inspect_run" --repo darkquasar/homebrew-tap \
  --name "distribution-authority-$inspect_run-$inspect_attempt" \
  --dir "$distribution_work/baseline-inspection"
cp "$distribution_work/baseline-inspection/distribution-authority.json" \
  "$distribution_work/baseline-candidate.json"
```

The CLI's inspect output file is the **identity candidate itself**, not a wrapper
and not a snapshot: `app`, repository, tag/version, peeled commit,
`checksums_sha256` and all six asset names/sizes/SHA-256s. Review the successful
run summary and candidate. Independently inspect tag/main ancestry, release
flags, immutable version URLs and downloaded binaries/checksums. Do not execute
binaries in the privileged controller. Compute `identity_digest` from the full
candidate using `canonical_digest` (same procedure as configuration hashing,
without `validate_configuration`). Present the exact candidate/digest to the
owner for explicit byte approval; a version choice alone is insufficient.

After that approval:

```sh
identity_digest=$(python3 -B - "$distribution_work/baseline-candidate.json" <<'PY'
import sys
from pathlib import Path
from distribution.snapshot import strict_json
from distribution.contracts import canonical_digest
print(canonical_digest(strict_json(Path(sys.argv[1]).read_bytes())))
PY
)
jq -n --arg app patronus --rawfile candidate "$distribution_work/baseline-candidate.json" \
  --arg digest "$identity_digest" \
  '{mode:"approve-baseline",app:$app,candidate:$candidate,approve_sha:$digest}' \
  > "$distribution_work/baseline-inputs.json"
gh workflow run distribute.yml --repo darkquasar/homebrew-tap \
  --ref distribution-control --json < "$distribution_work/baseline-inputs.json"
```

**This approval dispatch also publishes Brew/Scoop and prepares WinGet** after
freezing; it is not a dry run. It requires the separate publication grant. The
admitted authority job freshly re-downloads/re-inspects identity and compares the
owner-approved digest; changes fail. Baseline authorization records the actual
owner approval run/attempt, never a fabricated historical push. The immutable
record is `.distribution/releases/patronus/2.7.1.json` on
`distribution-records`, retaining original configuration/authorization and
snapshot digest. Existing equal identity reuses its original record; different
bytes/checksums/tag commit conflict. No overwrite/delete or silent re-approval.

## Second app: local/staging Lumen, never published

Use the accepted `lumen-native-registration.json` and `lumen-snapshot.json` under
`distribution/tests/fixtures/`, with different upstream `darkquasar/lumen`,
version `1.4.2`, command `lumen`, WinGet
`darkquasar.Lumen`, dedicated invented dispatcher `9002` / bot `8002`, six
`lumen-*` assets, and exact paths `Formula/lumen.rb`, `bucket/lumen.json`,
`manifests/d/darkquasar/Lumen/1.4.2/`. Fixture IDs, digests and approval refs are
inert; they must never be installed as real production registrations.

Generate each app/channel in an isolated candidate root, then combine validated
outputs into the **same** local tap/bucket/fork trees for discovery. Follow the
[native staging procedure](../distribution/tests/native/README.md#stage-two-apps-with-no-remote-publication)
to populate `staging/candidates/APP/CHANNEL` and `staging/catalogues/CHANNEL`.
Validate each app/channel through the real controller validation seam:

```sh
python3 -B -m distribution.cli validate --channel scoop --app lumen \
  --registration distribution/tests/fixtures/lumen-native-registration.json \
  --snapshot distribution/tests/fixtures/lumen-snapshot.json \
  --candidate-dir "$distribution_work/staging/candidates/lumen/scoop" --out "$distribution_work/lumen-scoop-validation.json"
```

Candidates must first contain only the exact
`render(channel, registration, snapshot)` path/byte outputs for that app/channel;
this command validates, it does not generate files. Keep result JSON outside
the candidate root; mixed catalogue trees are rejected by this strict seam. Repeat
with `brew` and `winget` using their matching candidate directories, and
Patronus's matching fixture pair. Native discovery and coexistence of a second
app come from the tap PR label `native-second-app`, which runs the per-release
matrix for Lumen while Patronus's staged entries share the tap and bucket.
Preservation acceptance comes from the label `native-lifecycle`, which runs the
one-time lifecycle for Patronus only; no Lumen lifecycle is claimed. See
[PR labels](../distribution/tests/native/README.md#pr-labels) and
[lifecycle acceptance](../distribution/tests/native/README.md#lifecycle-acceptance-pd-13).
Structural success is not authority or publication.
Do not push Lumen, create its remote repository/App or open its Microsoft PR.

For a future real second app, the owner must approve its actual distinct
repository/version/assets/metadata and dedicated dispatcher identity, install
the separate producer-reader App on that approved source, configure its own
main-only `package-dispatcher` environment, and implement/review its producer
receipt/dispatch path. The current Patronus producer is fixed to Patronus; a
registration alone does not make it a generic producer. Then use the same
owner registration/digest/application steps and an owner-cut release (or explicit
baseline). No new public tap/bucket URL or WinGet client source is needed.
Metadata-only changes may retain an existing snapshot only where its original
scope is compatible (presentation or owner-approved dispatcher replacement);
source/native identity/destination changes cannot reinterpret frozen history.

## Observe results and submit WinGet in the browser

Download the authority/publish artifacts for the exact run/attempt, retain control
SHA, registration digest, `record_ref`, protected record commit/snapshot digest,
channel definition digests/commits and WinGet `refs`. A selected-channel retry
must use this same identity; see [recovery](recovery.md). Three kinds of
evidence apply, and each stays distinct:

| Evidence | Where | What it shows |
|---|---|---|
| Structural validation | Channel `validation` in the publish result | Actual Ruby/Homebrew style plus pinned Scoop/WinGet schemas; missing validation/tool evidence blocks that channel |
| Hosted staging installation | The run's `native-checks` artifact (`native-checks.json`), referenced by each channel's `native_checks` (`run`, `digest`) | Install, `--version` and uninstall from a staging catalogue on every hosted platform before the channel writes |
| Live-source availability | The status rows below | The package as users receive it; neither check above proves it |

Also download the `native-checks` artifact for the run, for example
`gh run download "$controller_run" --repo darkquasar/homebrew-tap --name native-checks`.
A failed or missing native result stops only the affected channel with
`failed: native-check-failed` and `failing_platforms`; its previous definition
stays in place and the other channels proceed. That is a native install result,
not a structural-validation failure. See the
[native check procedure](../distribution/tests/native/README.md) and
[recovery for bottle and native-check failures](recovery.md#bottle-and-native-check-failures).

For a prepared WinGet branch, read the recorded upstream default/commit,
`upstream_base`, `head`/`controller_prepared_head` and `compare_url`. The whole diff
against that base must contain only the registered version's three manifests.
Open the returned comparison URL, for example:

`https://github.com/microsoft/winget-pkgs/compare/master...darkquasar:winget-pkgs:patronus/2.7.1?expand=1`

Use the actual upstream default if different. Logged in as the owner, inspect
base `microsoft/winget-pkgs`, head `darkquasar/winget-pkgs:patronus/2.7.1`,
version, URLs/hashes, validation results and changed files; click **Create pull
request**, complete Microsoft's current template and submit in the browser.
Record the PR URL/number/head SHA and follow normal GitHub notifications. CI
never creates/reopens the upstream PR. Reuse an existing exact-head open PR;
never duplicate it or reset moderator/owner edits.

| Observation | State / owner action |
|---|---|
| Validated fork branch + comparison link only | `prepared`; owner opens browser PR |
| Exact-head upstream PR open | `submitted`; follow GitHub feedback |
| PR merged, exact version absent from verified default source | `accepted-awaiting-indexing`; do not resubmit |
| Exact version observed in Microsoft's `winget` source | `published`; complete live install/version/lifecycle observation |
| PR closed unmerged | `rejected`; inspect feedback, approve metadata correction, explicit WinGet retry, then owner browser submission |
| API/client/source unavailable | `unknown`; preserve refs/last observation, investigate, no inferred publication or duplicate PR |

Use owner-only `status` on a **fresh** dispatch to refresh, including prior
unresolved submissions on that app. The Windows source job supplies current-run
observations automatically; there is no manual source evidence argument:

```sh
gh workflow run distribute.yml --repo darkquasar/homebrew-tap --ref distribution-control \
  -f mode=status -f app=patronus -f version=2.7.1
```

Status writes a public cache and uses the publisher role; it is not an offline
read-only command. No cron/extra monitoring is added. Setup may be accepted with
working Brew/Scoop and a real WinGet PR submitted-pending after other checks pass;
public WinGet installation remains pending until default-source evidence.

## Delivered controller CLI reference

The workflow translates the JSON inputs above into these actual maintainer
commands in its authenticated job context. They are not new `patronus` commands;
local invocation without GitHub run/ref/environment admission fails. Do not
fabricate `GITHUB_*` actor context or copy production keys locally.

```sh
python3 -B -m distribution.cli register --candidate configuration.json --approve-sha "$configuration_digest" --out approved-registration.json
python3 -B -m distribution.cli prepare --app patronus --run-id "$release_run" --attempt "$release_attempt" --out authority.json
python3 -B -m distribution.cli inspect-baseline --app patronus --tag v2.7.1 --out candidate.json
python3 -B -m distribution.cli approve-baseline --candidate candidate.json --approve-sha "$identity_digest" --out authority.json
python3 -B -m distribution.cli retry --app patronus --version 2.7.1 --channel winget --out result.json
python3 -B -m distribution.cli status --app patronus --version 2.7.1 --out status.json
```

`register` consumes the bare **configuration**, while its output is the approved
envelope. `inspect-baseline --out` emits the bare **identity**. `approve-baseline`
consumes that identity, not an existing snapshot envelope. Authority returns a
`record_ref`; publisher receives it through the isolated workflow, not a CLI
argument that grants authority. The real `validate` command above is the local
structural seam and is credential-free. For dispatch flags/JSON input, see
[GitHub CLI workflow run](https://cli.github.com/manual/gh_workflow_run).
