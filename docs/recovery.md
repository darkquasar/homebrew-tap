# Recover executable-package distribution

**PENDING live commissioning/native evidence.** These are later owner-operated
commands for the delivered controller; each needs its specific owner operation grant. Preserve
upstream executable releases, protected registrations/snapshots, successful
channels, other apps, unrelated files and users' content/state. An executable
package (Brew/Scoop/WinGet) is separate from a Patronus content package.

## Capture state before repair

Record affected app/version/channel, run/attempt URLs, source run/attempt,
control SHA, configuration digest, `record_ref`, protected records commit and
snapshot digest, current destination tips/markers/bytes, WinGet branch/compare/PR
refs and last observation. Download public artifacts while retained (30 days):

```sh
gh run view "$controller_run" --repo darkquasar/homebrew-tap \
  --json url,event,headSha,status,conclusion,jobs
gh run download "$controller_run" --repo darkquasar/homebrew-tap \
  --name "distribution-authority-$controller_run-$controller_attempt" --dir "$recovery_dir/authority"
gh run download "$controller_run" --repo darkquasar/homebrew-tap \
  --name "distribution-publish-$controller_run-$controller_attempt" --dir "$recovery_dir/publish"
gh run download "$controller_run" --repo darkquasar/homebrew-tap \
  --name "winget-source-$controller_run-$controller_attempt" --dir "$recovery_dir/source"
```

Set the run/attempt to the exact affected run and `recovery_dir` to a fresh owner
work directory outside checkouts. Some artifacts/jobs may be absent; record
that as missing evidence. Authority/publish results have channel failures and
`storage_errors` even when destination writes succeeded. A `write-outcome-unknown`
result retains candidate commit/ref information; read back before retrying.
Never post private keys/tokens in an incident note.

Read the protected record from the actual records tip (and recorded commit when
available), not from tap `main` or the public status cache. For Patronus 2.7.1:

```sh
records_tip=$(gh api repos/darkquasar/homebrew-tap/git/ref/heads/distribution-records --jq .object.sha)
gh api "repos/darkquasar/homebrew-tap/contents/.distribution/releases/patronus/2.7.1.json?ref=$records_tip" \
  --jq .content | base64 --decode > "$recovery_dir/protected-snapshot.json"
```

Use a platform's equivalent base64 decoder if needed. Retain its digest and Git
commit. The admitted CLI validates the envelope, initial configuration from
protected control history, authorization and all current release bytes; manually
reading JSON does not grant publication authority. Central status at
`.distribution/status/<app>/<version>.json` on the native tap default is a cache.
Brew/Scoop `.distribution/channels/<app>.json` markers are committed with their
definitions and must bind to protected records/actual bytes.

## Select a fresh-dispatch recovery path

```text
+-------------------+  => inspect record  +-------------------+
| Failure/interrupt | ==================> | Snapshot exists?  |
+-------------------+                     +-------------------+
                                             |          |
                         => yes, same bytes  |          | => no
                                             v          v
                               +------------------+  +------------------+
                               | Owner retry one  |  | Receipt intact?  |
                               | channel          |  +------------------+
                               +------------------+    |              |
                                                       | => yes       | => no
                                                       v              v
                                            +----------------+  +----------------+
                                            | Producer fresh |  | Owner inspect/ |
                                            | dispatch       |  | approve bytes  |
                                            +----------------+  +----------------+
```

1. **Protected snapshot exists:** repair the affected prerequisite and use an
   owner-only selected-channel retry at the current control tip. Authority
   reuses the original record; publisher rechecks frozen identity and current
   destinations. Do not refreeze from a replacement release or rebuild.
2. **No snapshot, intact original receipt:** start a new producer dispatch for
   the original successful source run/attempt, using approved main code:

   ```sh
   gh workflow run package-distribution.yml --repo darkquasar/patronus --ref main \
     -f run_id="$release_run" -f attempt="$release_attempt"
   ```

   The registered App sends controller `prepare`. Do not directly substitute
   the owner's actor for the bot. Inspect ambiguous dispatch results/receiver
   runs before requesting another; the sender may have succeeded.
3. **No snapshot, receipt missing/expired:** use [inspect/approve baseline](onboarding.md#historical-release-or-missing-receipt-inspect-and-approve-a-baseline)
   with explicit owner approval of exact bytes. Binaries remain downloadable;
   do not rebuild, move a tag or invent a historical release event.

If controller code/configuration moved after authority froze a record, publisher
fails freshness. Start a **new workflow_dispatch** at current
`distribution-control`; do not use `gh run rerun` to execute obsolete code or
relabel an old run as current. Owner-only recovery modes check both original and
triggering actor IDs. Bot-origin prepare rerun by owner/another app is denied.
Expired credentials/CAS exhaustion recover by the same fresh-dispatch path;
never refresh credentials through stale code.

## Retry one selected channel

After prerequisite repair and the applicable owner operation grant:

```sh
gh workflow run distribute.yml --repo darkquasar/homebrew-tap --ref distribution-control \
  -f mode=retry -f app=patronus -f version=2.7.1 -f channel=scoop
```

Choose exactly one of `brew`, `scoop`, `winget`. Version has no `v` prefix.
Retry is authenticated owner work, not a permission workaround or a dry run.
Inspect exact run results after it completes. It reconciles actual markers,
definitions and WinGet refs before writing, skips identical successful work,
retains other channels and other apps, and uses non-force CAS. Independent
channels can succeed despite another channel's failure; aggregate partial
failure remains nonzero. Never “rollback” by deleting successful definitions.
Older Brew/Scoop snapshots cannot lower a newer stable version; WinGet version
branches coexist. An identity conflict is a stop, even when both replacement
binaries and replacement checksums agree. A binary defect requires a newly
authorized version, not a same-version overwrite.

Presentation metadata correction requires owner approval of the new configuration
digest/envelope through register, then a normal protected control commit and a
fresh selected-channel retry. Current configuration digest is recorded with the
corrected channel; original snapshot authorization/configuration audit stays
unchanged. Source/native identity/destination changes cannot reinterpret an old
snapshot. Revalidate exact candidate bytes before writes; do not edit generated
files as an unrecorded bypass.

## WinGet feedback, fork sync and source uncertainty

| Condition | Recovery |
|---|---|
| Existing exact-head open PR | Reuse PR/reference, inspect feedback; do not automatically change submitted bytes or open a duplicate |
| Branch edited externally | Retain prior/current/submitted head SHAs and `externally_edited`; inspect moderator/owner changes, never reset the branch |
| Merged PR | Never resubmit; query indexing on a fresh status dispatch |
| Closed unmerged | Retain closed PR reference; owner approves correction, explicitly retries `channel=winget`, reviews new validated branch/compare link and submits in browser |
| Unreadable PR/source/client | Keep `unknown` and last refs/observation; diagnose before further action, never infer success or absence |
| `fork-sync-required` | Inspect denied operation and recorded Microsoft base, then owner performs bounded mirror sync below |

For `fork-sync-required`, confirm the actual upstream default recorded in the
result; normally `master`. With normal owner credentials and a separate sync
grant, fast-forward **that mirror branch explicitly**:

```sh
gh repo sync darkquasar/winget-pkgs --branch master --source microsoft/winget-pkgs
```

Never sync `winget-catalogue-docs`, force push, widen CI to Workflows write or
merge the fork's docs into a submission. If a fast-forward is impossible, stop
for owner disposition. A fresh `channel=winget` retry uses the same frozen
snapshot and re-resolves/records the actual Microsoft base. Its whole branch diff
must contain only the registered version manifest paths. Contents-only ref
feasibility remains a live commissioning check.

The owner alone opens the comparison link and creates the upstream PR in the
browser, then follows GitHub notifications. Retry never submits/reopens it.
The submitted/head history remains part of recovery evidence.

Refresh on the next release or explicit owner status request, not a timer:

```sh
gh workflow run distribute.yml --repo darkquasar/homebrew-tap --ref distribution-control \
  -f mode=status -f app=patronus -f version=2.7.1
```

The separate Windows source job automatically queries the protected package ID
with read-only `GITHUB_TOKEN`, no environment/App keys or client installation.
Actual client availability/query execution remains **pending**. Publisher accepts
only the current admitted run/attempt's successful Windows-job artifact, checked
API digest and reparsed output. Do not supply a manual observation JSON or
`source_evidence` input. Missing WinGet/source stays unknown while independent
Brew/Scoop publication can continue. A manual native diagnosis can run
`winget show --id darkquasar.Patronus --exact --source winget --versions`, but a
pasted output is not authenticated controller source evidence. Merged but absent
on a verified source query means `accepted-awaiting-indexing`; public install
guidance remains pending until exact default-source observation and live checks.

## Credentials and interruption incidents

To suspend new requests, disable/suspend the affected dispatch installation or
revoke its key in owner App settings, remove/replace only its
`package-dispatcher` environment secret and inspect in-flight runs. A private-key
rotation alone need not invalidate already minted installation tokens; revoke
those or suspend/uninstall the affected installation as needed. Then perform
fresh authorized recovery after investigating. Retain public snapshots/releases.

The owner accepted the initial Actions-write interruption risk: a stolen producer
dispatch credential can cancel/delete/rerun tap Actions and disrupt other apps'
updates/audit observations. It cannot write protected approvals or replace
release authority. Rotate only that producer's dispatcher key/registration if
needed; do not respond by issuing a broader shared key. Missing Actions evidence
can be recovered from protected records plus current destinations where present;
otherwise retain unknowns and use explicit baseline approval, never fabricate
lost evidence. There is no promise that Actions history can be reconstructed.

Revoke/rotate the catalogue publisher, authority recorder and producer reader
separately when each is affected, in their [listed environments](setup.md#exact-github-app-roles-and-placement).
Publisher compromise may corrupt native definitions/status but must not alter
control/records; verify GitHub rules, then reconcile against protected snapshots.
Authority/owner compromise affects release trust: suspend activation, inspect
protected history and obtain owner disposition rather than automatically blessing
records. Reader compromise grants source reads, not catalogue writes. Restore
only reviewed compatible controller code with a normal control-branch commit;
never force-reset approval/identity/state history. New control tip requires new
dispatch; protect schema/record compatibility on rollback.

Missing/expired secrets, unavailable environments, validation tools or server
policies fail visibly. Repair the exact configured role/policy, never fall back
to repository/org secrets, broad PATs, upstream credentials or permission-bypass
settings. Tokens/keys must not enter public artifacts/caches.

## Withdrawal and removal

Manager uninstall is consumer removal of that installation's executable and
links; it preserves content, harness files and Patronus state. A publishing
withdrawal is a separate owner-reviewed catalogue operation: follow Homebrew's
disable/removal policy for the tap formula, Scoop's manifest rules, or request
Microsoft-reviewed WinGet withdrawal/correction through the browser. Preserve
other apps, historical executable releases and frozen records. Do not force a
client downgrade, promise deletion of downloaded bytes or erase user state.
Consult [Homebrew deprecation/disable/removal guidance](https://docs.brew.sh/Deprecating-Disabling-and-Removing),
[Scoop manifest conventions](https://github.com/ScoopInstaller/Scoop/wiki/App-Manifests)
and [Microsoft contribution rules](https://github.com/microsoft/winget-pkgs/blob/master/CONTRIBUTING.md)
before a concrete withdrawal;
this runbook authorizes no destructive action. A corrected binary gets a new
owner-authorized release. Keep exact old/new definition digests and durable
withdrawal/feedback references.
