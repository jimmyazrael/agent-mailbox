#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from agent_chat import connect_db, send_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Post one mock agent message through agent_chat")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agent", choices=["claude", "codex"], required=True)
    parser.add_argument("--peer", choices=["claude", "codex"], required=True)
    parser.add_argument("--mode", choices=["continue", "final", "blocked"], default="continue")
    parser.add_argument("--summary", default="mock response")
    parser.add_argument("--body", default=None)
    parser.add_argument("--blocked-reason", default=None)
    parser.add_argument("--exit-after-post", action="store_true")
    args = parser.parse_args()

    body = args.body
    if body is None:
        body = f"mock {args.agent} {args.mode} response"
    conn = connect_db(args.root)
    try:
        send_message(
            conn,
            root=args.root,
            room_id=args.task_id,
            from_agent=args.agent,
            to_agent=args.peer,
            kind="message",
            status=args.mode,
            summary=args.summary,
            body=body,
            blocked_reason=args.blocked_reason,
            next_turn=args.peer if args.mode == "continue" else None,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
