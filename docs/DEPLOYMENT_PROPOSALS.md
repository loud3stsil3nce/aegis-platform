# Immutable deployment proposals

Phase 7 separates source changes, image publication, approval, and runtime
replacement. No model-facing MCP tool receives GitHub write credentials, a
registry credential, the Docker socket, or an unrestricted Compose command.

## Boundary sequence

1. A reviewed change reaches an exact Git commit on `main`.
2. GitHub Actions builds only `examples/hello-aegis` for `linux/amd64`.
3. The workflow uses its ephemeral `GITHUB_TOKEN` with only Contents read and
   Packages write. The Change Proposals GitHub App receives no Packages or
   Workflows permission.
4. BuildKit publishes a `git-<40-character-sha>` tag with provenance and SBOM.
   The deployment input is the returned `repository@sha256:<digest>`, never the
   tag alone.
5. The deployment policy binds that digest to one plugin/service, the exact Git
   commit, the currently expected immutable image, rollback image, and bounded
   health timeout.
6. A requester creates the durable proposal and a different actor approves it.
7. The executor rechecks the current image before atomically consuming approval.
8. An internal authenticated proxy pulls the exact digest and recreates only the
   allowlisted Compose service with `--no-deps --no-build --force-recreate`.
9. The executor requires both a healthy service and the exact approved image.
   Failure restores and verifies the bound previous digest; uncertain rollback
   stops for manual recovery.

## Bootstrap gates

The workflow is intentionally outside the source-change App's allowlisted paths
and requires Workflows permission that the App does not have. A repository
maintainer must therefore review and bootstrap it independently. Do not expand
the Change Proposals App merely to bypass this separation.

The current production `hello-aegis` image is the mutable local reference
`aegis/hello-aegis:0.1.0`. Normal deployment proposals require an immutable
current image, so the first transition to GHCR is a one-time migration. It must
have a separately approved record of the legacy image, target digest, health
gate, and rollback procedure. Subsequent deployments use the ordinary immutable
proposal path.

The checked-in GitHub workflow uses BuildKit provenance and SBOM. GitHub's
hosted artifact-attestation documentation states that attestations for private
repositories require GitHub Enterprise Cloud. Add the GitHub attestation step
only after confirming account support; never treat provenance as a substitute
for vulnerability scanning or review.

References:

- https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions
- https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations

## Non-goals

- No default-branch write or merge capability
- No generic Docker, exec, volume, network, or remove endpoint
- No multi-service deployment
- No mutable image tag as a deployment input
- No production activation before CI publication, vulnerability scanning,
  Compose validation, explicit bootstrap approval, and rollback evidence
