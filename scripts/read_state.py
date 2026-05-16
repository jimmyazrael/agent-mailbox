from __future__ import annotations

import argparse
import json

from mailbox_common import add_common_args, load_state, read_messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Read mailbox state and recent messages")
    add_common_args(parser)
    parser.add_argument("--latest", type=int, default=1, help="Number of latest messages to print")
    args = parser.parse_args()

    state = load_state(args.root, args.task_id)
    print(json.dumps(state, indent=2, sort_keys=True))
    messages = read_messages(args.root, args.task_id)
    if args.latest:
        print("\n# Latest messages")
        for msg in messages[-args.latest :]:
            print(f"\nMSG {msg.get('message_id')} {msg.get('author')} -> {msg.get('target')} [{msg.get('status')}]")
            print(msg.get("content", ""))


if __name__ == "__main__":
    main()

