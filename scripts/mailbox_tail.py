from __future__ import annotations

import argparse
import time

from mailbox_common import add_common_args, messages_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Tail mailbox messages")
    add_common_args(parser)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    path = messages_path(args.root, args.task_id)
    if not args.follow:
        print(path.read_text(encoding="utf-8"), end="")
        return

    pos = 0
    while True:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                print(chunk, end="", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

