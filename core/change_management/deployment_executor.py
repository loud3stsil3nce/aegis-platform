"""One-service immutable deployment executor with mandatory rollback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .deployment_policy import ImmutableDeploymentPlan
from .deployment_proposals import DeploymentProposalStore


class DeploymentExecutionError(RuntimeError):
    pass


class DeploymentAdapter(Protocol):
    """Narrow runtime boundary; implementations must affect only ``service``."""

    def current_image(self, service: str) -> str: ...

    def deploy_image(
        self, service: str, image_reference: str, idempotency_key: str,
    ) -> None: ...

    def wait_healthy(self, service: str, timeout_seconds: int) -> bool: ...


@dataclass(frozen=True)
class DeploymentResult:
    proposal_id: str
    plugin_id: str
    service: str
    image_reference: str
    status: str


class OneServiceDeploymentExecutor:
    def __init__(self, *, store: DeploymentProposalStore, adapter: DeploymentAdapter):
        self.store = store
        self.adapter = adapter

    def execute(
        self, *, proposal_id: str, plan: ImmutableDeploymentPlan, actor: str,
    ) -> DeploymentResult:
        if not actor:
            raise DeploymentExecutionError("executing actor is required")

        current_image = self.adapter.current_image(plan.service)
        self.store.consume(
            proposal_id, plan=plan, current_image=current_image, actor=actor,
        )

        deployment_attempted = False
        try:
            deployment_attempted = True
            self.adapter.deploy_image(
                plan.service, plan.image_reference, f"{proposal_id}:deploy",
            )
            if not self.adapter.wait_healthy(plan.service, plan.health_timeout_seconds):
                raise DeploymentExecutionError("deployed service did not pass its health gate")
            if self.adapter.current_image(plan.service) != plan.image_reference:
                raise DeploymentExecutionError("healthy service does not run the approved image")
        except Exception as deployment_error:
            if not deployment_attempted:
                raise
            try:
                self.adapter.deploy_image(
                    plan.service, plan.rollback_image, f"{proposal_id}:rollback",
                )
                rollback_healthy = self.adapter.wait_healthy(
                    plan.service, plan.health_timeout_seconds,
                )
                rollback_exact = self.adapter.current_image(plan.service) == plan.rollback_image
            except Exception as rollback_error:
                self.store.record_execution(
                    proposal_id, status="FAILED", actor=actor,
                    detail="deployment failed and rollback operation failed; manual recovery required",
                )
                raise DeploymentExecutionError(
                    "deployment and rollback failed; manual recovery required"
                ) from rollback_error
            if not rollback_healthy or not rollback_exact:
                self.store.record_execution(
                    proposal_id, status="FAILED", actor=actor,
                    detail="deployment failed and rollback verification failed; manual recovery required",
                )
                raise DeploymentExecutionError(
                    "deployment failed and rollback could not be verified; manual recovery required"
                ) from deployment_error
            self.store.record_execution(
                proposal_id, status="ROLLED_BACK", actor=actor,
                detail="deployment health gate failed; prior immutable image restored",
            )
            raise DeploymentExecutionError(
                "deployment failed health verification and was rolled back"
            ) from deployment_error

        self.store.record_execution(
            proposal_id, status="HEALTHY", actor=actor,
            detail="approved immutable image passed the service health gate",
        )
        return DeploymentResult(
            proposal_id, plan.plugin_id, plan.service, plan.image_reference, "HEALTHY",
        )
