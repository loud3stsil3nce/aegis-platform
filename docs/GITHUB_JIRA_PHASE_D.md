# GitHub–Jira Phase D: Governed Merge and Immutable Deployment

Phase D completes the closed-loop autonomous engineering lifecycle. It establishes
strict, role-separated governance for merging approved draft pull requests into `main`
and deploying immutable container artifacts to Zenbook Server with mandatory health
gates and verified rollback references.

---

## 1. Governance & Security Boundaries

1. **Separation of Duties (Dual-Approver Rule):**
   - The actor approving the PR merge (`merge-approver`) must be strictly distinct
     from the actor who requested the change proposal (`requester`) and the actor
     who approved the initial draft snapshot (`approver`).
   - Self-approval and role overlap fail closed.

2. **Branch Protection Enforcement:**
   - GitHub active ruleset `22242875` (`Protect main via reviewed pull requests`)
     mandates that no commit can enter `main` without an approved human pull request
     review and resolved conversations.
   - The merge command verifies through GitHub's pull request reviews API that at
     least one approving review exists before initiating the merge.

3. **Atomic Merge Operation:**
   - Merging is performed via GitHub REST API (`PUT /repos/{owner}/{repo}/pulls/{number}/merge`).
   - Squash merge produces a single immutable Git commit on `main`.
   - The commit message permanently binds the Jira issue key, proposal ID, review
     binding SHA-256, and approving actor.

4. **Immutable Container Deployment:**
   - Deployments target allowlisted services declared in `config/deployment-policy.json`.
   - The deployment plan durably binds:
     - Exact Git commit SHA from the merged PR
     - Immutable target image reference/digest
     - Expected current running image
     - Rollback image reference
     - Bounded health timeout
   - Execution passes through the authenticated Docker deployment proxy with
     `--no-deps --no-build --force-recreate`.
   - Health gate verification (`wait_healthy`) and running image ID verification
     must both pass. Failure triggers immediate automatic rollback to the bound
     rollback reference.

5. **Jira Issue Lifecycle Closure:**
   - On successful merge, Jira issue transitions to `aegis:merged`.
   - On successful deployment health gate verification, Jira issue receives the
     deployment receipt and audit record, transitioning to `Resolved` / `Done`.

---

## 2. Operator Workflow & Commands

### Step 1: Merge Approved Pull Request

```sh
# Merge approver: verify ruleset compliance and merge PR into main
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/merge-approver \
  merge --proposal <proposal-id> --binding <reviewed-binding-sha256>

# Notify Jira of successful merge
python3 aegis-platform/scripts/jira_change_proposal.py \
  --config /operator/config.json \
  --actor-token-file /operator/secrets/merge-approver \
  notify-merge --proposal <proposal-id>
```

### Step 2: Governed Immutable Deployment

Deployments use `ImmutableDeploymentService` (`core/change_management/deployment_service.py`):

```sh
# Deploy approver & executor roles govern the immutable rollout
# 1. Propose rollout binding the merged git SHA and target image digest
# 2. Approve rollout with independent approver identity
# 3. Execute rollout via deployment proxy; verify health check and container state
```

---

## 3. Exit Gates

- [x] Merge requires explicit `merge-approver` role.
- [x] Requester self-approval rejected.
- [x] Altered or expired binding rejected.
- [x] Ruleset review compliance verified before GitHub merge dispatch.
- [x] Merge commit SHA and PR number recorded in immutable proposal store.
- [x] Jira receives merge notification and audit evidence.
- [x] Deployment plan binds exact Git commit and image digest.
- [x] Health gate failure triggers automatic verified rollback.
