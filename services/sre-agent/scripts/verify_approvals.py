"""Verify exact-action approval lifecycle without persisting test data."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.approvals import consume_approval, create_approval, decide_approval
from src.db.database import async_session


async def main() -> None:
    async with async_session() as session:
        approval = await create_approval(
            session,
            action_type="restart_application",
            target="verification-only",
            arguments={"service": "verification-only"},
            risk_class="R3",
            policy_version="verification-v1",
            requested_by="verification-requester",
        )
        await decide_approval(
            session,
            approval_id=approval.id,
            approver="verification-approver",
            approve=True,
        )
        await consume_approval(
            session,
            approval_id=approval.id,
            action_type="restart_application",
            target="verification-only",
            arguments={"service": "verification-only"},
        )
        if approval.status != "CONSUMED":
            raise RuntimeError(f"unexpected approval status: {approval.status}")
        await session.rollback()
        print("approval lifecycle verified; transaction rolled back")


if __name__ == "__main__":
    asyncio.run(main())
