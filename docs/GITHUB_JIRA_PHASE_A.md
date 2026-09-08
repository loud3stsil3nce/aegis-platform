# GitHub–Jira Phase A: read-only incident diagnosis

Phase A turns a failed GitHub workflow/check for
`loud3stsil3nce/aegis-platform` into one deduplicated Jira issue. A human must
then add the configured `@sre-agent` mention before the service reads bounded
GitHub evidence and posts a diagnosis. This feature has no branch, commit, pull
request, merge, deployment, shell, filesystem-write, or Docker mutation path.

## Security boundary

Create a dedicated private GitHub App named `Aegis GitHub Incident Reader`.
Do not reuse the `Aegis Change Proposals` App or a personal access token.

Install the App only on `loud3stsil3nce/aegis-platform` and grant exactly:

| Repository permission | Access |
| --- | --- |
| Metadata | Read |
| Actions | Read |
| Checks | Read |
| Contents | Read |
| Pull requests | Read |

Subscribe only to `Workflow run` and `Check run` events. Give the App a unique,
high-entropy webhook secret. If the Zenbook endpoint is not publicly reachable,
leave active delivery disabled and use the bounded polling recovery path until a
reviewed ingress exists. The local signed-webhook fixture still verifies HMAC,
replay rejection, and event parsing.

The attached official GitHub MCP Server documentation recommends explicit
read-only mode and narrow toolsets. An optional operator-side GitHub MCP must
therefore set all three of the following:

```text
GITHUB_READ_ONLY=1
GITHUB_LOCKDOWN_MODE=1
GITHUB_TOOLSETS=actions,repos,pull_requests
```

The automated Phase A service uses a smaller REST surface: token creation plus
repository-scoped GETs for workflow/check state, failed jobs, bounded logs,
commits, associated pull requests, and check runs. It never gives the model the
App token. Lockdown remains defense in depth; all GitHub and Jira text is still
handled as untrusted data.

## Runtime configuration

Keep values in an owner-only deployment environment or secret file:

```text
AEGIS_GITHUB_ALLOWED_REPOSITORIES=loud3stsil3nce/aegis-platform
AEGIS_GITHUB_READ_APP_ID=<numeric app id>
AEGIS_GITHUB_READ_INSTALLATION_ID=<numeric installation id>
AEGIS_GITHUB_READ_PRIVATE_KEY_HOST_PATH=<owner-only host PEM path>
AEGIS_GITHUB_WEBHOOK_SECRET_HOST_PATH=<owner-only high-entropy secret file>
AEGIS_SRE_PHASE_A_IMAGE=<immutable source-derived SRE image tag>
AEGIS_GITHUB_POLL_ENABLED=1
AEGIS_GITHUB_POLL_INTERVAL_SECONDS=60
AEGIS_GITHUB_POLL_LOOKBACK_MINUTES=15
```

Merge the deployment override with the base Compose file so the private key and
independent webhook secret are mounted read-only and are not copied into an
image, environment value, or repository. Both host files must have mode 0600.
The override also removes the mutable `/app` source bind and requires an exact
image tag; the repository remains mounted read-only at `/app/code` for bounded
diagnostic context:

```text
docker compose -f docker-compose.yml \
  -f deploy/github-jira-phase-a.compose.yml config
```

The existing Jira variables remain required: `JIRA_URL`, `JIRA_USER_EMAIL`,
`JIRA_API_TOKEN`, and `JIRA_PROJECT_KEY`. The workflow fails closed and returns
503 while any required setting or key file is absent.

The reviewed Phase A candidate is
`aegis/sre-agent:phase-a-2f8f3e40ae7c`, source SHA-256
`2f8f3e40ae7c5ff96f7216c0a13dbe37a3fe4c0321899d5885f6f62c1858fff0`,
image ID `sha256:16fe920fee25686a313c8f608ec27f2e259c7a1c800ad1683ea2dc552afd8482`.
Its runtime image deliberately omits Docker CLI and compiler binaries. Preserve
the pre-Phase-A SRE image as
`aegis/sre-agent:rollback-pre-phase-a-729b28aa3b20` before recreation.

## Phase A exit evidence

Phase A passed on 2026-09-07 with Jira issue
[`KAN-126`](https://rafiurrahman325.atlassian.net/browse/KAN-126):

- the signed fixture created one issue and one incident;
- replay plus a second delivery produced no duplicate (`event_count=2`);
- human comment 10324 invoked the SRE trigger;
- one fixture-aware, evidence-bound diagnosis advanced the incident to
  `DIAGNOSED` and replaced the investigate label with `aegis:diagnosed`;
- the audit sequence is `DETECTED,TRIAGED,DETECTED,DIAGNOSED` and SQLite
  integrity is `ok`;
- unsigned webhook requests return 401;
- the exact candidate remains healthy with zero restarts, no broad PAT, no
  Docker socket, and no GitHub or Zenbook mutation during diagnosis.

The repository has no failed workflow/check history, so the required synthetic
fixture was used rather than manufacturing a GitHub failure. Its SHA-256 is
`a69e08bc5b84b4a6d7c308d9043b75a74a8adf17777ec4a15a64cb8d3e5559ee`.

## Durable state and event behavior

- HMAC-SHA256 is verified over the raw request body before JSON parsing.
- Each `X-GitHub-Delivery` ID is claimed once in SQLite.
- Repository, workflow/check, commit SHA, and run ID form a stable SHA-256
  incident fingerprint.
- Different webhook deliveries and recovery polls for the same fingerprint
  converge on one Jira issue.
- A fingerprint label lets startup/crash recovery find an issue that Jira
  created before the local issue binding was committed.
- Jira receives metadata and safe evidence links, never raw logs or credentials.
- Failed-job logs are capped at 64 KiB/400 lines, redacted, and fetched from an
  allowlisted HTTPS GitHub object-storage redirect without forwarding the App
  authorization header.
- External text cannot inject the Jira agent mention into a created issue or
  diagnosis, preventing self-trigger loops.
- Investigation records an evidence hash and the durable `DIAGNOSED` state.

## Verification

Run the dependency-free contract and synthetic integration suite:

```text
PYTHONPATH=services/sre-agent \
  python3 -m unittest services/sre-agent/tests/test_github_jira_phase_a.py -v
```

The fixture at
`services/sre-agent/tests/fixtures/github_workflow_failure.json` proves the
Phase A application flow. The exit-gate sequence is:

1. Submit the fixture with a correct signature and a unique delivery ID.
2. Confirm one Jira issue contains only structured metadata and safe links.
3. Replay the same delivery and submit a second delivery with the same incident
   fingerprint; confirm no additional issue is created.
4. Add a human-authored `@sre-agent investigate` comment.
5. Confirm one evidence-linked read-only diagnosis is posted and the incident
   becomes `DIAGNOSED`.
6. Confirm no GitHub repository state or Zenbook service/container identity
   changed.

Do not proceed to Phase B until the same sequence passes against the configured
GitHub App and Jira project.
