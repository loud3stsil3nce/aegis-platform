"""Durable exact-action approval primitives.

No mutating tool is registered yet. These primitives are the only approval
mechanism permitted when the approved-restart milestone is implemented.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import secrets

from sqlalchemy import select, update

from src.db.models import ActionApproval


TERMINAL_STATUSES = frozenset({"REJECTED", "EXPIRED", "CONSUMED", "CANCELLED"})


def canonical_arguments(arguments: dict) -> str:
    if not isinstance(arguments, dict):
        raise TypeError("approval arguments must be an object")
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def arguments_hash(arguments: dict) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode()).hexdigest()


def new_nonce_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


async def create_approval(
    session,
    *,
    action_type: str,
    target: str,
    arguments: dict,
    risk_class: str,
    policy_version: str,
    requested_by: str,
    ttl_seconds: int = 900,
) -> ActionApproval:
    if risk_class not in {"R3", "R4"}:
        raise ValueError("durable approvals are limited to R3/R4 actions")
    if ttl_seconds < 30 or ttl_seconds > 3600:
        raise ValueError("approval TTL must be between 30 and 3600 seconds")
    now = datetime.datetime.utcnow()
    approval = ActionApproval(
        action_type=action_type,
        target=target,
        arguments=arguments,
        arguments_hash=arguments_hash(arguments),
        nonce_hash=new_nonce_hash(),
        risk_class=risk_class,
        policy_version=policy_version,
        requested_by=requested_by,
        status="PENDING",
        created_at=now,
        expires_at=now + datetime.timedelta(seconds=ttl_seconds),
    )
    session.add(approval)
    await session.flush()
    return approval


async def decide_approval(
    session,
    *,
    approval_id: str,
    approver: str,
    approve: bool,
    reason: str | None = None,
) -> ActionApproval:
    now = datetime.datetime.utcnow()
    result = await session.execute(
        select(ActionApproval).where(ActionApproval.id == approval_id).with_for_update()
    )
    approval = result.scalar_one_or_none()
    if approval is None:
        raise ValueError("approval not found")
    if approval.status != "PENDING":
        raise ValueError(f"approval is already {approval.status}")
    if approval.expires_at <= now:
        approval.status = "EXPIRED"
        approval.decided_at = now
        raise ValueError("approval expired")
    if secrets.compare_digest(approval.requested_by, approver):
        raise ValueError("requester cannot approve their own action")
    approval.status = "APPROVED" if approve else "REJECTED"
    approval.approved_by = approver
    approval.decided_at = now
    approval.decision_reason = reason
    await session.flush()
    return approval


async def consume_approval(
    session,
    *,
    approval_id: str,
    action_type: str,
    target: str,
    arguments: dict,
) -> None:
    """Atomically consume one approval bound to the exact action and arguments."""
    now = datetime.datetime.utcnow()
    statement = (
        update(ActionApproval)
        .where(
            ActionApproval.id == approval_id,
            ActionApproval.status == "APPROVED",
            ActionApproval.expires_at > now,
            ActionApproval.action_type == action_type,
            ActionApproval.target == target,
            ActionApproval.arguments_hash == arguments_hash(arguments),
        )
        .values(status="CONSUMED", consumed_at=now)
    )
    result = await session.execute(statement)
    if result.rowcount != 1:
        raise ValueError("approval is invalid, expired, altered, or already consumed")
