"""Actor-authorized application service for governed restart workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .restart import ACTION_TYPE, ApprovalError, RestartExecutor, RestartProposalStore, RestartResult


class ActorAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    actor_id: str
    roles: frozenset[str]


class DeploymentService:
    def __init__(self, store: RestartProposalStore, executor: RestartExecutor):
        self.store = store
        self.executor = executor

    @staticmethod
    def _require(actor: Actor, role: str) -> None:
        if not actor.actor_id or role not in actor.roles:
            raise ActorAuthorizationError(f"actor is not authorized as {role}")

    def propose(self, *, plugin_id: str, actor: Actor, ttl_seconds: int = 900) -> dict[str, Any]:
        self._require(actor, "requester")
        hook = self.executor.hooks.get(plugin_id)
        if not hook or hook.get("action") != ACTION_TYPE:
            raise ApprovalError("plugin has no declared restart lifecycle hook")
        target = str(hook["target"])
        timeout = int(hook.get("healthTimeoutSeconds", hook.get("timeout_seconds", 60)))
        state = self.executor.adapter.state(target)
        expected = {"status": state.get("status"), "started_at": state.get("started_at")}
        proposal_id = self.store.propose(
            plugin_id=plugin_id, target=target, requested_by=actor.actor_id,
            expected_state=expected, timeout_seconds=timeout, ttl_seconds=ttl_seconds,
        )
        return self.store.get(proposal_id)

    def approve(self, proposal_id: str, *, actor: Actor) -> dict[str, Any]:
        self._require(actor, "approver")
        proposal = self.store.get(proposal_id)
        try:
            self.store.approve(proposal_id, approved_by=actor.actor_id)
        except ApprovalError:
            # Expiry is recorded atomically by the store. Other approval
            # denials retain an attributable event without changing state.
            current = self.store.get(proposal_id)
            if current["status"] != "EXPIRED":
                self.store.record(
                    proposal_id, proposal["plugin_id"], proposal["target"],
                    "APPROVAL_DENIED", actor.actor_id, "approval rejected by policy",
                )
            raise
        return self.store.get(proposal_id)

    def execute(self, proposal_id: str, *, actor: Actor) -> RestartResult:
        self._require(actor, "executor")
        proposal = self.store.get(proposal_id)
        try:
            return self.executor.execute(
                proposal_id, plugin_id=proposal["plugin_id"],
                arguments=proposal["arguments"], actor=actor.actor_id,
            )
        except ApprovalError:
            self.store.record(
                proposal_id, proposal["plugin_id"], proposal["target"],
                "EXECUTION_DENIED", actor.actor_id, "execution rejected by policy",
            )
            raise
