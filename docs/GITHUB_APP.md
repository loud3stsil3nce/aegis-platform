# Aegis GitHub App Boundary

## Initial installation scope

Create one private GitHub App owned by `loud3stsil3nce` and install it on only
`loud3stsil3nce/aegis-platform`. Do not select all repositories. ReefTracker,
E2EE Messaging, and Shariah Compliant Screener remain independent integrations
and require separate future installation approval.

Recommended name: `Aegis Change Proposals` (GitHub may require a globally unique
suffix). Disable user authorization and webhooks for the initial pull-request
workflow. Do not configure a callback URL or webhook secret yet.

Repository permissions:

| Permission | Access | Purpose |
| --- | --- | --- |
| Metadata | Read | GitHub-required repository identity and state |
| Contents | Read and write | Read the base revision and create/update only governed proposal branches |
| Pull requests | Read and write | Create and inspect proposal PRs after durable approval |

Set every other repository, organization, account, and administration
permission to `No access`, including Actions, Workflows, Deployments, Packages,
Secrets, Environments, Webhooks, and Administration. A future CI/deployment
milestone must request those permissions separately and pass a fresh permission
review.

## Defense in depth

GitHub permissions cannot restrict `Contents: write` to particular paths or
branches. Aegis therefore applies the external policy in
`config/change-policy.json` before generating a token or Git request:

- exact repository `loud3stsil3nce/aegis-platform`;
- protected base branch `main`, never a write target;
- proposal branches under `aegis/` only;
- explicitly allowed Core paths, excluding all service submodules;
- no binary patches or traversal;
- at most 20 files, 128 KiB of diff, and 1,000 changed lines.

The App private key belongs in an owner-only server file and never in Git,
Compose environment, Docker inspection output, model context, or audit detail.
Installation access tokens must be minted on demand, narrowed again to the
single repository and required permissions, held only in memory, and discarded
after their one-hour maximum lifetime. Code must not assume a fixed token
length or format.

## Deliberately disabled

- Direct writes to `main`
- Merge and auto-merge
- Workflow file changes
- Releases and package publication
- Deployment creation or environment changes
- Repository settings, collaborators, secrets, and hooks
- Access to service-submodule repositories
- Use of the legacy `GITHUB_PAT`

The first live GitHub gate is read-only installation verification. Branch, commit,
and pull-request creation remain disabled until token containment, exact-head
preconditions, durable approval, replay, and permission-negative tests pass.
