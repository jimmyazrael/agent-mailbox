from __future__ import annotations

import argparse
import sys

from mailbox_common import add_common_args, post_message


def main() -> None:
    parser = argparse.ArgumentParser(description="Post one manual mailbox message")
    add_common_args(parser)
    parser.add_argument("--author", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--status", choices=["continue", "blocked", "final", "error"], default="continue")
    parser.add_argument("--summary", default="")
    parser.add_argument("--content-file", help="Read content from file instead of stdin")
    parser.add_argument("--next-turn", help="Next participant when status=continue")
    args = parser.parse_args()

    if args.content_file:
        with open(args.content_file, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = sys.stdin.read()
    state = post_message(
        root=args.root,
        task_id=args.task_id,
        author=args.author,
        target=args.target,
        status=args.status,
        summary=args.summary,
        content=content,
        next_turn=args.next_turn,
    )
    print(f"posted message_id={state['last_message_id']} status={state['status']} turn={state.get('turn')}")


if __name__ == "__main__":
    main()

