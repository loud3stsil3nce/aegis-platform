"""Role-separated application service for immutable one-service deployments."""

from __future__ import annotations

from typing import Any

from .deployment_auth import DeploymentActor, DeploymentActorAuthorizationError
from .deployment_executor import OneServiceDeploymentExecutor
from .deployment_policy import DeploymentPolicy, DeploymentPolicyError, ImmutableDeploymentPlan
from .deployment_proposals import DeploymentApprovalError, DeploymentProposalStore


class ImmutableDeploymentService:
    def __init__(
        self, *, policy: DeploymentPolicy, store: DeploymentProposalStore,
        executor: OneServiceDeploymentExecutor,
    ):
        self.policy = policy
        self.store = store
        self.executor = executor

    @staticmethod
    def _require(actor: DeploymentActor, role: str) -> None:
        if not actor.actor_id or role not in actor.roles:
            raise DeploymentActorAuthorizationError(f"actor is not authorized as {role}")

    @staticmethod
    def _plan(record: dict[str, Any]) -> ImmutableDeploymentPlan:
        return ImmutableDeploymentPlan(
            record["plugin_id"], record["service"], record["git_sha"],
            record["image_reference"], record["expected_current_image"],
            record["rollback_image"], record["health_timeout_seconds"],
        )

    def propose(
        self, *, plugin_id: str, git_sha: str, image_digest: str,
        health_timeout_seconds: int, ttl_seconds: int, actor: DeploymentActor,
    ) -> dict[str, Any]:
        self._require(actor, "requester")
        target = self.policy.targets.get(plugin_id)
        if target is None:
            # Keep unknown targets outside the runtime adapter boundary.
            raise DeploymentPolicyError("plugin is not an allowlisted deployment target")
        current_image = self.executor.adapter.current_image(target.service)
        plan = self.policy.plan(
            plugin_id=plugin_id, git_sha=git_sha, image_digest=image_digest,
            expected_current_image=current_image,
            health_timeout_seconds=health_timeout_seconds,
        )
        return self.store.propose(plan=plan, requested_by=actor.actor_id, ttl_seconds=ttl_seconds)

    def get(self, proposal_id: str, *, actor: DeploymentActor) -> dict[str, Any]:
        if not actor.roles.intersection({"requester", "approver", "executor"}):
            raise DeploymentActorAuthorizationError("actor cannot inspect deployments")
        proposal = self.store.get(proposal_id)
        if actor.roles == frozenset({"requester"}) and proposal["requested_by"] != actor.actor_id:
            raise DeploymentActorAuthorizationError("requester cannot inspect another actor's deployment")
        return proposal

    def approve(self, proposal_id: str, *, actor: DeploymentActor) -> dict[str, Any]:
        self._require(actor, "approver")
        proposal = self.store.get(proposal_id)
        current_image = self.executor.adapter.current_image(proposal["service"])
        try:
            return self.store.approve(
                proposal_id, approved_by=actor.actor_id, current_image=current_image,
            )
        except DeploymentApprovalError:
            current = self.store.get(proposal_id)
            if current["status"] not in {"EXPIRED", "STALE"}:
                self.store.record_denial(
                    proposal_id, status="APPROVAL_DENIED", actor=actor.actor_id,
                )
            raise

    def execute(self, proposal_id: str, *, actor: DeploymentActor):
        self._require(actor, "executor")
        proposal = self.store.get(proposal_id)
        return self.executor.execute(
            proposal_id=proposal_id, plan=self._plan(proposal), actor=actor.actor_id,
        )
