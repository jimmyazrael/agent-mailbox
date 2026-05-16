from __future__ import annotations

import argparse
from pathlib import Path

from mailbox_common import add_common_args, init_mailbox


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize an agent mailbox task")
    add_common_args(parser)
    parser.add_argument("--goal", required=True, help="Task goal passed to the first agent turn")
    parser.add_argument("--project-cwd", type=Path, default=Path.cwd(), help="Project working directory for agent subprocesses")
    parser.add_argument("--participants", default="codex,claude", help="Comma-separated participants")
    parser.add_argument("--first-turn", default="codex", help="Participant that acts first")
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--turn-timeout-seconds", type=int, default=300)
    parser.add_argument("--max-peer-message-chars", type=int, default=10000)
    parser.add_argument("--max-total-cost-usd", type=float, default=5.0)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite existing task state/messages")
    args = parser.parse_args()

    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    state = init_mailbox(
        root=args.root,
        task_id=args.task_id,
        goal=args.goal,
        project_cwd=args.project_cwd,
        first_turn=args.first_turn,
        participants=participants,
        max_rounds=args.max_rounds,
        turn_timeout_seconds=args.turn_timeout_seconds,
        max_peer_message_chars=args.max_peer_message_chars,
        max_total_cost_usd=args.max_total_cost_usd,
        max_total_tokens=args.max_total_tokens,
        force=args.force,
    )
    print(f"initialized {args.task_id}: turn={state['turn']} status={state['status']}")


if __name__ == "__main__":
    main()

