#!/usr/bin/env python3
"""Prove a consumed change proposal cannot be consumed again."""

from __future__ import annotations

import argparse
import json

from core.change_management import ChangeApprovalError, ChangeProposalStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--proposal-id", required=True)
    args = parser.parse_args()
    store = ChangeProposalStore(args.state)
    try:
        proposal = store.get(args.proposal_id)
        try:
            store.consume(
                args.proposal_id, diff_sha256=proposal["diff_sha256"],
                content_manifest_sha256=proposal["content_manifest_sha256"],
                current_base_sha=proposal["base_sha"], actor="github-executor",
            )
        except ChangeApprovalError:
            status = "rejected"
        else:
            raise RuntimeError("consumed proposal replay unexpectedly succeeded")
        events = [dict(row) for row in store.connection.execute(
            "SELECT status,actor FROM change_events WHERE proposal_id=? ORDER BY created_at,event_id",
            (args.proposal_id,),
        )]
        print(json.dumps({"replay": status, "proposalStatus": store.get(args.proposal_id)["status"], "events": events}, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
