# GitHub–Jira Phase B: operator-approved draft proposals

Status: **local implementation and contract verification complete (2026-09-08)**.
Live activation and a separately approved Jira-linked draft PR remain pending.
No live GitHub write, Jira update, merge, deployment, or SRE restart was performed
during this implementation. Phase A's reader App and runtime credential boundary
are unchanged. `callscope` and service submodules are outside this work.

## Boundary

1. A human applies `aegis:github-proposal` to a durable GitHub incident and sends
   a new `@sre-agent` mention. A label alone does not initiate processing.
2. SRE records `PROPOSAL_REQUESTED` once and posts an `aegis:approval-required`
   acknowledgement. This is **not approval**, and creates no GitHub branch.
3. A trusted operator prepares a bounded exact file snapshot using the separate
   command below. Repository and incident fingerprint come from the durable
   Phase A database, not editable Jira descriptions. The current Jira label is
   checked again. The operator supplies the exact current `main` SHA; it need
   not equal the historical failure's commit.
4. GitHub reads use explicitly narrowed installation tokens. The adapter walks
   the exact base tree, rejects truncated trees and nonordinary files, and
   checks blob bytes against their Git object SHA. The review diff is generated
   from these base bytes and the proposed content.
5. The durable store binds Jira issue/fingerprint, repository, `main`, base SHA,
   governed branch, paths, diff hash, content-manifest hash, complete before/after
   snapshots, requester, policy version, expiry, and recovery metadata into one
   review binding hash. The approver must inspect and supply that exact hash.
6. The independent approver uses a separate authenticated command. An executor
   then atomically consumes the approval once. It accepts no replacement files,
   diff, repository, title, branch, or PR body from Jira or the executor.
7. The adapter rechecks `main` before and after minting the write token. A final
   snapshot/approval/expiry check occurs immediately before the first blob
   write. The token is narrowed to exactly `loud3stsil3nce/aegis-platform`,
   Contents write, Pull requests write, and Metadata read, with a validated
   remaining lifetime of at most one hour.
8. Execution creates blobs, one tree, one commit parented at the approved SHA,
   one new `aegis/jira-*` branch, and one **draft** PR targeting `main`.
   It never updates `main`, merges, dispatches workflows, changes settings or
   secrets, or deploys. Commit SHA, branch, PR URL/number and actor are audited.
9. A separate `notify` operation posts the durable result and audit ID to the
   same Jira issue and applies `aegis:pr-open`. Notification cannot rerun GitHub.

This is an operator-assisted proposal workflow, not automatic patch generation.
The SRE model cannot access the writer credential, approve proposals, or invoke
the operator command. No new HTTP or MCP mutation endpoint is exposed.

## Supported snapshot scope

- Exactly the platform repository and `main` as base.
- Existing external `config/change-policy.json` path allowlist applies.
- New or modified ordinary `100644` text files only.
- UTF-8, LF, final newline for nonempty text; NUL and CR are rejected.
- No deletion, rename, executable mode, symlink, submodule or workflow change.
- At most 20 files, 128 KiB proposed content, 128 KiB base content, 128 KiB diff,
  1,000 changed lines, and eight path components. Configured tighter diff/file
  budgets still apply. Requests/timeouts are bounded; writes are never retried.
- One active proposal per Jira issue. Phase C now provides explicit local
  abandonment and new-ID/new-branch retry with fresh approval; see
  [GITHUB_JIRA_PHASE_C.md](GITHUB_JIRA_PHASE_C.md). Never edit an approved row
  or reuse its branch. Live Phase B/C acceptance remains pending.
- Expiry is 30–3,600 seconds from preparation, default 900 seconds.

Jira stores hashes and safe machine metadata, not raw code snapshots or logs.
The protected operator review output contains exact code, including untrusted
text; do not feed it to a tool-enabled model as instructions. Review for secrets
before preparing any snapshot.

## Operator setup (not activated automatically)

Use the existing **Aegis Change Proposals** App, not the Phase A reader:
App ID `4821358`, installation `158855695` as recorded in the progress log.
Reverify the installation and exact permissions before live use. Do not expand
the reader App or mount the writer key into SRE/Open WebUI.

Run the command on a trusted operator host with Python 3.10+ (the production
baseline is Python 3.12), OpenSSL and the existing pinned `jira` dependency.
The operator runtime requires:

- Reviewed source and `config/change-policy.json`.
- Read-only access to the Phase A incident database or a consistent SQLite
  backup including the newly recorded proposal request. Do not copy only a live
  database file while ignoring its WAL.
- A separate `0700` proposal-state directory; DB, operator config and credentials
  are owner-only, owned by the operator runtime user.
- Three distinct high-entropy operator tokens (minimum 32 bytes): requester,
  approver, executor. One role per identity. The approval actor differs from
  the requester. Never put tokens in command arguments or commit them.
- Owner-only writer private-key file.
- `JIRA_URL`, `JIRA_USER_EMAIL`, `JIRA_PROJECT_KEY`, and
  `JIRA_API_TOKEN_FILE` for `prepare`/`notify`. The token value is file-backed.
  `inspect` needs neither Jira nor a GitHub credential; approval/execution need
  GitHub only.

Example **nonsecret structure** for an owner-only operator configuration file;
replace paths with reviewed host-specific absolute paths:

```json
{
  "state": "/operator/state/github-jira-proposals.sqlite3",
  "incidentStore": "/operator/read-only/github-jira-phase-a.sqlite3",
  "policy": "/operator/code/config/change-policy.json",
  "appId": 4821358,
  "installationId": 158855695,
  "privateKeyFile": "/operator/secrets/github-change-app.pem",
  "actors": [
    {"id": "proposal-requester", "roles": ["requester"], "tokenFile": "/operator/secrets/requester"},
    {"id": "repository-owner", "roles": ["approver"], "tokenFile": "/operator/secrets/approver"},
    {"id": "github-executor", "roles": ["executor"], "tokenFile": "/operator/secrets/executor"}
  ]
}
```

The trusted OS/operator runtime owns configuration, code, DB and credential
files. These are not an adversarial multi-user API: someone with arbitrary write
access to the DB/code/config or access to every credential is inside the trust
boundary. Hash binding detects altered execution inputs/records, not an attacker
who controls all records and can rewrite every hash. Do not hand this filesystem
or the other roles' tokens to an untrusted requester.

## Commands

From the Dev workspace (or use the script's absolute path on the operator host):

```sh
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/requester \
  prepare --issue KAN-123 --base-sha <exact-current-main-sha> \
  --snapshot /operator/review/snapshot.json --ttl 900
```

Snapshot JSON maps repository paths to exact full contents:

```json
{"docs/example.md": "# Reviewed example\n"}
```

Preparation writes a durable proposal but does not approve or create a PR.
Save the returned proposal ID and binding SHA-256, then inspect:

```sh
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/approver \
  inspect --proposal <proposal-id>
```

The human reviews the full `snapshot.diff`, before/after content, repository,
base SHA, paths, expiry and rollback metadata. Only after exact approval:

```sh
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/approver \
  approve --proposal <proposal-id> --binding <reviewed-binding-sha256>

python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/executor \
  execute --proposal <proposal-id> --binding <reviewed-binding-sha256>

python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/executor \
  notify --proposal <proposal-id>
```

The older Phase 7 `execute_github_proposal.py` bootstrap script is **not** the
Phase B interface; it combines proposal and approval and must not be registered
with the agent or substituted for this split workflow.

## Failure and recovery

- Missing/altered approval, binding, target, content or expiry fails closed.
  Changed live base fails before writes. Obtain fresh human review rather than
  changing stored snapshots.
- Consumption and its audit commit before dispatch. An audit failure blocks
  GitHub writes. Multiple executors or replay after process restart have at most
  one consumption winner.
- A crash between preparation and snapshot binding may leave an orphan PENDING
  proposal. It cannot execute through Phase B without a bound snapshot/receipt.
  Inspect it; do not manufacture approval.
- After `CONSUMED`, never rerun writes, including on timeouts or loss of the PR
  response. The outcome may be indeterminate. Preserve the exact branch and
  inspect GitHub read-only. Unlike the legacy adapter's optional cleanup,
  Phase B does **not delete branches** after ambiguous PR failures.
- The rollback record identifies the approved base SHA and governed branch.
  The base is never mutated and needs no automatic reset. Any abandonment or
  deletion is a separately reviewed operator action.
- Jira request acknowledgements and result notifications are claimed before
  posting to avoid duplicate storms. A timeout may leave a claimed request or
  `SENDING` notification without a visible comment. Inspect Jira and durable
  state and reconcile manually; do not reset claims blindly.
- Preparation may commit before its Jira summary is delivered. Find the
  proposal by `jira_change_snapshots.issue_key`; do not prepare replacement
  content. Failures are not automatically interpreted as permission to retry.
- GitHub has no cross-request transaction locking the base branch. It can move
  after the final check. The created commit still has the exact approved parent,
  and no base write is made. Read-only post-verification is required before any
  later merge decision.

## Verification and remaining live gate

Local checks:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aegis-platform \
  python3 -m unittest discover -s aegis-platform/core/change_management/tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aegis-platform/services/sre-agent:aegis-platform \
  python3 -m unittest discover -s aegis-platform/services/sre-agent/tests
```

Tests cover exact stored-snapshot reproduction with the real Git-object adapter
and fake transport, independent approval, target/base/content/diff/rollback/
expiry/approval tampering, expiry during token minting, scope isolation, prompt
injection as content, audit failure, concurrent consumption, replay after
restart, byte/mode/tree/hash checks, token expiry, PR failure and Jira recovery.

**Live Phase B exit evidence is still required:**

1. Separately approve deployment of the request-only SRE changes, with exact
   candidate image, rollback and source evidence; do not add a writer key.
2. Bootstrap the separate trusted operator configuration/state/credentials.
3. Create a real request on the selected Jira incident, prepare a reviewed
   minimal docs-only snapshot against current `main`, and obtain exact human
   approval of its binding hash.
4. Execute once, verify the actual PR is open/draft with the exact base,
   branch/head, file bytes and diff; link it to Jira and verify durable audit.
5. Demonstrate replay rejection and preserved base/no merge/no deployment.
   Keep CI feedback and merge/deployment outside Phase B.