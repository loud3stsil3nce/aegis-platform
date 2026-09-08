# GitHub–Jira Phase C: CI feedback and governed retry

Status (2026-09-08): implemented and contract-tested locally. **Live Phase B/C
acceptance is not complete.** No live GitHub/Jira write, deployment, container
restart, App setting change, merge, or branch deletion was performed during
this implementation. Final read-only SSH preflight verified `sre_agent` healthy
on `zenbook-server`, Docker context `default`, running
`aegis/sre-agent:phase-a-2f8f3e40ae7c`. The earlier name filter used a hyphen
instead of an underscore and matched nothing; it was not evidence of an outage.
This verifies container health, not fresh end-to-end GitHub/Jira functionality.

## Boundaries

- Phase B remains operator-assisted: a requester supplies exact full file
  contents, an independent approver reviews the binding, and an executor
  consumes approval once. Automatic model-generated fixes are not implemented.
- Phase C adds deterministic SRE CI summaries, not a model/tool execution loop.
- The writer App, actor tokens, approval database and exact code snapshots stay
  outside SRE. SRE receives a trusted metadata-only manifest mounted read-only.
- GitHub evidence reads are GET-only. Token minting requests repository-exact
  read permissions; no workflow dispatch, PR close, delete, merge or deploy
  method is added.
- A CI pass is evidence at an observed head, **not merge authorization**, proof
  of required-check coverage, or an immutable guarantee that CI cannot change.
  Human branch-protection and merge review remain independent.

## Operator lifecycle

Use `scripts/jira_change_proposal.py` and the configuration/roles documented in
[GITHUB_JIRA_PHASE_B.md](GITHUB_JIRA_PHASE_B.md).

New commands:

```sh
# Approver: retire this exact local authorization.
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/approver \
  abandon --proposal <old-id> --binding <old-reviewed-binding>

# Requester: explicitly bind a fresh proposal to its abandoned predecessor.
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/requester \
  prepare --issue KAN-123 --base-sha <current-main-sha> \
  --snapshot /operator/revised-files.json \
  --retry-of <old-id> --retry-binding <old-reviewed-binding>
```

Then inspect, independently approve and execute the **new** proposal normally.
The new proposal has a new ID, branch, binding and expiry; old receipts do not
carry over. One issue can have only one active bound proposal and each
predecessor can have only one successor. Original snapshots, hashes, audit and
remote branches/PRs are preserved.

`abandon` is an approver-authenticated local state transition, not permission
to close a remote PR or delete anything. An already consumed proposal with no
durable result is indeterminate and cannot be abandoned/retried by this CLI.
Inspect remote state and audit manually; do not manufacture a result or reset
consumption. Expired/stale unconsumed proposals can be abandoned and replaced.

The schema migrates Phase B's unique issue constraint transactionally while
preserving existing snapshot payloads and receipts. Back up the operator SQLite
database before first production use. A preparation race/crash can still leave
a safe orphan proposal without a bound snapshot; that orphan cannot execute
through this interface.

## Publishing the CI manifest

After execution, and after every abandonment, use the executor identity:

```sh
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/executor \
  export-ci
```

Output is JSON only: version, proposal/issue IDs, repository, reviewed binding,
base/head/branch/PR metadata, lifecycle state and expected Git blob hashes.
There are no code contents, actor tokens, private keys or approval receipts.

The trusted operator must validate and atomically publish this output as
`manifest.json` in a dedicated directory mounted read-only into SRE. Write a
temporary file, check command success and JSON validation, then rename within
that directory; never truncate the live manifest before export succeeds.
Mount the **directory**, not an individual inode, so atomic replacement is
visible in the container. Do not mount the operator secrets/state directory.

The manifest is an OS-trusted input, not cryptographically signed. Hashes
detect changes to previously observed identities but do not protect against an
attacker controlling both operator input and SRE state. It must never be
populated from Jira text or model output. Keep the host directory
operator-controlled and non-writable to SRE, with files not group/world writable.

Publish abandonment and allow its Jira notice to complete before preparing the
successor. After successor execution publish the full export again. Historical
abandonment comments do not replace an active executed successor's CI label.
Pending unexecuted proposals are intentionally not exported; the Phase B
preparation notice remains their human-visible approval state.

## Runtime activation (not performed)

Existing Phase A environment/read-key/Jira configuration remains required.
Add only these settings to a separately reviewed immutable SRE candidate:

```text
AEGIS_GITHUB_CI_ENABLED=1
AEGIS_GITHUB_CI_MANIFEST=/run/aegis-ci/manifest.json
AEGIS_GITHUB_CI_STORE=/app/state/github-jira-phase-c.sqlite3
AEGIS_GITHUB_CI_INTERVAL_SECONDS=60
```

Mount the dedicated manifest directory at `/run/aegis-ci:ro`, and retain the
existing durable `/app/state` volume. CI defaults to disabled; the scheduler
uses one coalesced job instance. Disabling CI does not disable Phase A polling.

**Additional reader permission prerequisite:** the read App installation must
allow **Commit statuses: read** (`statuses: read`) in addition to Actions,
Checks, Contents, Metadata and Pull requests read. Phase C requests and checks
this exact set with a token lifetime greater than 30 seconds and at most one
hour. Phase A still requests its existing narrower permission set. Requesting
the extra permission in code does not grant it on GitHub: an App owner must
review/change permissions and approve installation updates before activation.
Never solve a 403 by passing the writer token or a PAT to SRE.

Provision the separate Phase B trusted operator runtime and complete its real
draft-PR gate first. Deployment requires exact candidate image/source,
expected current image, health checks, rollback and separate durable approval.
This document is not deployment authorization. No Compose override was applied.

## Evidence, bounds and states

One proposal is observed per scheduler tick in least-recently-checked order.
With N entries, a normal cycle takes approximately N intervals, plus API time.
Maximum manifest: 100 entries / 256 KiB. Export refuses more than 100 stored
bound proposals; archive/retention requires operator review rather than silent
omission.

An active PR observation makes at most five bounded JSON API reads:

1. Exact PR repository, number, URL, branch, base SHA, head SHA, open/unmerged
   and draft state.
2. Exact commit, single approved parent, exact changed path set and Git blob
   hashes. Only additions/modifications are accepted.
3. Latest check runs at that head.
4. Combined legacy commit statuses at that head.
5. Recheck PR identity/head/base after collection.

Responses are capped at 1 MiB each; CI uses zero transport retries and 15-second
socket timeouts. Counts must match response arrays. Responses at or above the
100-result page cap, missing counts, malformed IDs or mismatched heads fail
closed. No unbounded pagination or log download is performed. Socket timeouts
are not a strict aggregate wall-clock deadline; no hard process watchdog is
introduced here.

| State | Jira behavior |
| --- | --- |
| `CI_VERIFIED` | All observed results successful and nonempty; request human review |
| `CI_FAILED` | Failure/error/cancel/timeout; request human review and explicit fresh proposal if needed |
| `CI_PENDING` | Work incomplete or no checks present; do not claim success |
| `REVIEW_REQUIRED` | Missing/inaccessible/truncated/stale evidence, changed PR, skipped/neutral/unknown conclusions |
| `ABANDONED` | Authorization retired; branch/PR preserved; no GitHub reads needed |

Summaries contain counts, controlled links, exact head/binding and delivery ID.
External check names, logs, bodies and arbitrary URLs are not copied. The
watcher never adds an agent mention. CI verifies head/parent/path/blob evidence;
it is not a replacement for a complete human review of tree modes, required
checks, repository policy or the eventual merge diff.

## Durable delivery and recovery

`ci_tracking` binds each observed proposal identity and remembers the last
delivered evidence. `ci_deliveries` stores attributable payloads, delivery IDs,
timestamps and `SENDING`/`SENT` status. A repeated unchanged poll, including after
restart, does not post again. A later pass → fail → pass transition posts each
change. A newer concurrent poll supersedes an older in-flight observation.

A Jira write is claimed before dispatch. Any ambiguous comment/label response
leaves `SENDING` and blocks later deliveries for that issue. Inspect the
comment's CI delivery ID and actual labels, then reconcile under operator
review. Do not automatically reset the claim or replay comments. This chooses
at-most-once dispatch over pretending exactly-once delivery is possible.
Keep the scheduler single-instance during manifest publication/reconciliation;
remote APIs and local databases have no shared transaction.

## Verification and live exit checklist

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aegis-platform \
  python3 -m unittest discover -s aegis-platform/core/change_management/tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aegis-platform/services/sre-agent:aegis-platform \
  python3 -m unittest discover -s aegis-platform/services/sre-agent/tests
```

Local tests exercise operator snapshot → approval → draft-result export →
real watcher with fake transport → success/failure/abandonment Jira messages,
legacy migration, role and retry gates, immutable predecessor history, token
permissions/lifetime, concurrency, replay, and ambiguous delivery.

Remaining live acceptance:

1. Reverify the expected current immutable image ID and rollback before any
   deployment. Container identity/health was verified read-only: `sre_agent`,
   context `default`, Phase A image `aegis/sre-agent:phase-a-2f8f3e40ae7c`.
2. Locate/provision the separate trusted operator configuration; the two guessed
   config paths and the historical platform CLI path checked were absent.
3. Verify reader/writer App installations and independently authorize any
   necessary `statuses: read` installation update.
4. Complete an exact human-approved minimal draft PR and Phase B postverification.
5. Approve/activate the immutable SRE candidate plus read-only manifest mount.
6. Observe real passing **and** failing PR CI evidence update the same Jira
   issue correctly; record PR/head/check IDs and durable delivery/audit IDs.
7. Exercise abandonment/new-ID retry with fresh human approval, preserve the
   original PR, and demonstrate replay rejection with no merge/deploy effect.

No live Phase B/C completion may be claimed from fixture tests alone.
Phase D merge/deployment automation remains disabled and outside this task.