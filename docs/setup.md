# Executable-package setup

**PENDING commissioning and publication.** This is the provisioning runbook for
executable packages (Brew/Scoop/WinGet), distinct from Patronus content packages.
It describes later owner-authorized operations; writing these docs provisions
nothing. No npm channel/account, upstream PR token, extra submission account,
self-hosted WinGet source or recurring monitor is required.

Keep README candidates local when the native/default documentation branches
are not yet provisioned. Verify actual remote refs/settings before applying them;
never put the WinGet catalogue README on its Microsoft mirror or submission refs.

## Repository and branch map

| Repository | Branch | Purpose / destination |
|---|---|---|
| `darkquasar/patronus` | `main` | Approved producer code, `.github/workflows/release.yml` and `package-distribution.yml` |
| `darkquasar/homebrew-tap` | `distribution-control` | Trusted `distribution/`, workflow, schema/dependency pins and `.distribution/registrations/<app>.json` |
| Same tap | `distribution-records` | Data-only, create-once `.distribution/releases/<app>/<version>.json`; no workflows/code |
| Same tap | Future native default `main` | `Formula/`, README, channel markers, public status cache, secret-free workflow registration entry |
| `darkquasar/scoop-bucket` | `main` | `bucket/`, README and channel markers |
| `darkquasar/winget-pkgs` | Future default `winget-catalogue-docs` | Catalogue README only; never a submission base |
| Same fork | `master` (verify upstream's actual default) | Preserved Microsoft mirror; owner syncs explicitly if needed |
| Same fork | `<app>/<version>` | Exactly the app/version manifests, based on a recorded Microsoft commit |

Read actual settings before changing anything:

```sh
gh api repos/darkquasar/homebrew-tap --jq '{full_name,default_branch}'
gh api repos/darkquasar/scoop-bucket --jq '{full_name,default_branch}'
gh api repos/darkquasar/winget-pkgs --jq '{full_name,default_branch,fork,parent:.parent.full_name}'
gh api repos/microsoft/winget-pkgs --jq '{full_name,default_branch}'
gh api repos/darkquasar/homebrew-tap/branches --paginate --jq '.[].name'
```

Only `darkquasar`, numeric ID `28435534`, initially approves setup,
registrations/authority changes and releases. Stop on identity/namespace conflicts.
Commissioning follows accepted integration/checks/aggregate review, a separate
provisioning grant and a separate exact producer PR merge grant. A feature branch
or local commit does not activate the main-branch `workflow_run` dispatcher.

## Provision in this order, before exposing keys

1. Preserve existing histories/content. In owner-managed clean checkouts, seed
   the native tap `main`, data-only `distribution-records`, Scoop `main` and
   fork `winget-catalogue-docs` branches from reviewed candidates. Set the native
   tap default to `main`, Scoop to `main`, fork to `winget-catalogue-docs`. Retain
   fork `master` as its Microsoft mirror. Use normal owner commits/pushes and
   explicit ref names, never force or history replacement.
2. Deploy the reviewed controller to `distribution-control`. Put the reviewed
   secret-free `.github/workflows/distribute.yml` registration entry on tap
   `main`, with the same nine inputs: `mode`, `app`, `run_id`, `attempt`, `tag`,
   `version`, `channel`, `candidate`, `approve_sha`. It must have no privileged
   jobs/keys and must not check out controller code for execution. GitHub needs
   this default-branch entry to register dispatch. Every real dispatch selects
   `--ref distribution-control`; the default entry is not trusted execution.
3. Create and install the distinct Apps on their selected repositories, recording
   public numeric IDs/slugs/installations. **Do not generate private keys yet.**
   The Apps must exist and be installed before the rulesets can name them.
4. Install the active branch rulesets below, including the named App bypass
   actors. Create the environments with exact branch-type deployment policies.
   Inspect inherited settings and confirm first-revision fresh-ref/actor admission.
   If the account cannot enforce this separation, stop activation and return the
   limitation to the owner; Python path checks cannot replace native server rules.
5. Only after those controls are in place, generate each App's private key and
   upload it solely to the listed environment. Keep automatic per-release operation;
   no new required-reviewer or wait-timer gate is prescribed. Record policy exports
   and branch SHAs, never key/token bytes.
6. Under the commissioning grant, run the server-denial probes below before
   activation; then follow [onboarding](onboarding.md). Do not dispatch real
   publication merely to check that docs are writable.

### Seed records without inheriting controller code

Only if the remote records branch is absent, use a new dedicated owner checkout
outside existing work. Set `records_seed_checkout` to a new directory. The
no-checkout clone plus orphan branch avoids copying control workflows/files:

```sh
git clone --no-checkout --single-branch --branch distribution-control \
  https://github.com/darkquasar/homebrew-tap.git "$records_seed_checkout"
git -C "$records_seed_checkout" switch --orphan distribution-records
git -C "$records_seed_checkout" commit --allow-empty -m 'Initialize data-only distribution records'
git -C "$records_seed_checkout" ls-tree -r --name-only HEAD
git -C "$records_seed_checkout" push origin HEAD:refs/heads/distribution-records
```

The tree listing must be empty. An existing branch is preserved and inspected,
never reset/recreated. Subsequent authority commits add only release JSON.
Do not use the UI's “branch from default” operation for records. Use equivalent
new orphan branches for unprovisioned native/docs branches, adding only their
reviewed README/native-layout/registration-entry candidates. Inspect each full
tree before normal owner commit/push. No command here grants these remote writes.

### Identify the default-branch registration entry exactly

Obtain the reviewed execution-evidence bundle from the owner; set
`execution_evidence` to its `evidence/` directory. The accepted registration-entry
candidate is `t4/source-job-default-entry.yml`, SHA-256
`7baa81ed630a70700cb343a5237ff40eab4e9d765b6e91c86344cf85206b4a55`.
Verify that file's SHA-256 (`sha256sum` or `shasum -a 256`) before copying it to
`.github/workflows/distribute.yml` in the owner-managed tap **native main**
checkout, alongside the native README candidate. The privileged control-branch
workflow is not this entry. Record the resulting blob/commit; a missing or
mismatched candidate blocks this setup step and returns to the owner integration
package rather than authoring a substitute workflow in a documentation task.

After reviewed branches exist, the separately authorized owner sets their
repository defaults explicitly (Settings > General > Default branch, or):

```sh
gh repo edit darkquasar/homebrew-tap --default-branch main
gh repo edit darkquasar/scoop-bucket --default-branch main
gh repo edit darkquasar/winget-pkgs --default-branch winget-catalogue-docs
```

## Exact GitHub App roles and placement

Create Apps through owner Settings > Developer settings > GitHub Apps. Use
selected-repository installations, repository permissions below, no organization
or account permissions, and no event subscriptions/webhook requirement for these
outbound Apps. GitHub supplies Metadata read. Record each App's actual numeric
ID; do not confuse it with its client ID. See
[GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app).

| App role | Selected installation repositories | Repository permissions | Repository / environment; variables; secret |
|---|---|---|---|
| One dispatcher per producer | Tap only: `darkquasar/homebrew-tap` | **Actions write**; no Contents write | Producer / `package-dispatcher`; variable `PACKAGE_DISPATCH_APP_CLIENT_ID` (client ID); secret `PACKAGE_DISPATCH_APP_PRIVATE_KEY` |
| Authority recorder | Tap only | **Contents write** | Tap / `package-authority`; variable `PACKAGE_AUTHORITY_APP_ID` (numeric App ID); secret `PACKAGE_AUTHORITY_APP_PRIVATE_KEY` |
| Catalogue publisher | Tap, `darkquasar/scoop-bucket`, `darkquasar/winget-pkgs` only | **Contents write** | Tap / `package-publisher`; variable `PACKAGE_PUBLISH_APP_ID`; secret `PACKAGE_PUBLISH_APP_PRIVATE_KEY` |
| Separate producer reader | Approved producer(s), initially `darkquasar/patronus`; no catalogue write installation | **Actions read, Contents read** | Tap / `package-authority`; variable `SOURCE_READ_APP_ID`; secret `SOURCE_READ_APP_PRIVATE_KEY` |

The producer reader is a different App from the authority recorder. Authority
`prepare` mints a repository-selected read token to fetch the producer's exact
run/attempt/receipt; other authority modes do not use this reader. Install it on
each subsequently approved producer; never expand either writing App into source
repositories. Publisher tokens are selected per destination repository and status
uses a tap-only token. Do not give any role Workflows, Administration or
Environments write. Never grant Microsoft upstream write or use a classic PAT.

Generate each App's private key in its settings; upload its PEM through the
corresponding environment secret UI or `gh secret set --repo REPO --env ENV NAME`
from a protected local file/stdin. Do not put keys in command arguments, logs,
Git, fixtures or artifacts. No equivalent repository secret or inherited
organization secret may supply these keys; inspect and remove conflicting
placements under owner authority. Variables are public identifiers, not secrets.

## Environment branch policies

Repository Settings > Environments > the named environment > Deployment branches
and tags: choose **Selected branches and tags**, add an exact **branch** rule,
then place the table's secrets/variables inside that environment. Do not use
“Protected branches only,” wildcard names or tag rules. See
[GitHub environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).

| Repository | Environment | Only allowed deployment branch |
|---|---|---|
| Patronus (each producer independently) | `package-dispatcher` | `main` (or its explicitly approved default branch) |
| Tap | `package-authority` | `distribution-control` |
| Tap | `package-publisher` | `distribution-control` |

A tag named `distribution-control`, default/records branch or PR ref must not
reach the tap keys. `DISTRIBUTION_ENVIRONMENT` is also checked by code; the
server policy protects secrets before that code runs. Repository-wide fallback
keys would undo this boundary. Fork/PR CI remains secret-free/read-only.

## Branch rulesets

Settings > Rules > Rulesets > New branch ruleset: set enforcement **Active**,
target each exact `refs/heads/...` name, and use separate update/create and
no-force/no-delete rulesets. In the update/create rulesets, enable Restrict
creations and Restrict updates; grant only the listed actors an **Always** bypass.
On this personal repository, use Repository admin only for the trusted owner;
verify no unintended administrators. Exclude generic write/maintain roles and
the GitHub Actions integration. Keep the independent no-force/no-delete
ruleset's bypass list empty, including for Apps and owner/admin. These controls
compose; an update bypass must not bypass the history protections. See
[GitHub rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

| Ref / repository | Create/update bypass actors | Independent rules, no bypass |
|---|---|---|
| Tap `distribution-control` | Owner/admin only; **no App** | Restrict deletions; Block force pushes |
| Tap `distribution-records` | Owner/admin + authority App only | Same |
| Tap native `main` | Owner/admin + publisher App only; exclude authority | Same |
| Scoop native `main` | Owner/admin + publisher App only | Same |
| WinGet fork default `winget-catalogue-docs` | Owner/admin only; no App | Same |

Contents write is repository-wide, not a path grant. Registered-path checks
limit normal writes but do not protect unguarded refs against a stolen token.
Record all active rulesets (not Evaluate), inherited rules and bypass actors.
Do not add an “all branches” update restriction that prevents the publisher's
registered WinGet submission branches; submission refs use non-force CAS and
exact manifest-diff checks. Preserve the mirror and existing histories.

Before activation, use legitimate short-lived role tokens and reviewed benign
commit objects for server probes: publisher update/create/delete/force against
both protected tap refs denied; authority update control/native main denied;
force/delete denied for either writer on protected/default refs. Record actual
request/ref/status and unchanged tips, without credentials. Demonstrate authorized
record and native definition writes too. An unexpected success blocks activation;
restore only that benign probe with owner authorization, correct rules and
repeat. Also exercise a denied default-branch/tag dispatch without privileged
token minting. Offline fixtures do not prove GitHub enforcement. Actual
Contents-only WinGet fork ref feasibility also remains **pending** until this
commissioning, not a reason to add Workflows write.

## Jobs, admission and record handoff

```text
+-------------------+  ~> workflow_run       +-------------------+
| Producer [CI]     | ~~~~~~~~~~~~~~~~~~~~~> | Dispatcher [main] |
+-------------------+                        +-------------------+
                                                       | => registered App
                                                       v
+-------------------+  <= record CAS         +-------------------+
| Records [tap]     | <===================== | Authority [tap]   |
+-------------------+                        +-------------------+
          | => protected readback                      | => record ready
          v                                            v
+-------------------+  <= source artifact    +-------------------+
| Publisher [tap]   | <===================== | Source [Windows]  |
+-------------------+                        +-------------------+
          | => validated files
          v
+-------------------+
| Catalogues        |
+-------------------+
```

Tap authority and publish jobs each have a 20-minute limit, immutable checkout,
no persisted checkout credentials and read-only Contents/Actions `GITHUB_TOKEN`.
They independently require the correct repository, workflow path/event, exact
branch ref, current control tip and API run/attempt. `prepare` requires both
original and triggering actor to equal the registered dispatcher bot; its App
ID/slug is checked. Register, inspect/approve baseline, retry and status require
both actor IDs to equal owner `28435534`. Owner rerunning a bot-origin prepare
run is denied. Missing context/credentials or stale code fails closed.

The authority job freezes all six assets plus checksum identity before writes.
Its token-free `record_ref` names app/version, controller/configuration digests,
records commit and snapshot digest. It passes through `needs.authority.outputs`;
publisher independently reads protected control history and both the named
reachable records commit/current records tip, checks original authority and
current release bytes, renders/validates, then mints only its publishing tokens.
Public status cache, workflow outputs and default-branch JSON do not grant trust.
No authority key, producer read key or downloaded producer program enters the
publishing runner. Tokens are revoked at job completion; expiry must cover the
remaining job bound, and stale code cannot refresh them.

The owner-approved `winget-source` job is **secret-free**, on `windows-2025`,
10-minute bound, Contents/Actions read `GITHUB_TOKEN`, no environment/App key or
package-client installer. It admits actual controller context and protected registration,
queries `winget show --id ID --exact --source winget --versions` with
`--accept-source-agreements --disable-interactivity`, and retains
bounded command/client/runner diagnostics in `winget-source-<run>-<attempt>`.
Publisher verifies the current admitted run/attempt/control SHA, successful
completed Windows job, API artifact digest and reparsed query bytes; there is no
caller-supplied `source_evidence` input. Client/query execution is **pending**:
the published runner inventory does not establish WinGet availability. Missing
client/source or a failed query stays `unknown`. Brew/Scoop can still continue
when this job fails; no automatic installer or privilege expansion is permitted.

## Rotation and acceptance evidence

For routine rotation, generate a new key for the same App, replace only its
listed environment secret, run a fresh authorized operation under the applicable
grant, then revoke the old key. Existing installation tokens may live until
revoked/expiry; revoke affected installations/tokens promptly for compromise.
Stop affected dispatches during an incident. Changing dispatcher App/bot identity
also requires owner registration approval and a fresh control dispatch; changing
a same-App private key does not change identity. Rotate authority, publisher and
producer-reader keys independently; see [recovery](recovery.md).

Before declaring setup accepted, retain exact policy/role/ref denial evidence,
producer merge/workflow SHAs, approved registration and snapshot digests,
per-channel commits/results, native lifecycle observations and the owner's real
WinGet browser PR URL. Brew/Scoop must be usable and other setup checks passed;
WinGet may be `submitted` pending Microsoft, not merely `prepared`. Keep public
WinGet install guidance pending until default-source visibility and native
installation are observed. No documentation/static check satisfies these live gates.
