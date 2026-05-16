from __future__ import annotations

import argparse
from pathlib import Path

from mailbox_common import copytree_replace, skill_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Install agent-mailbox skill for Codex and Claude Code")
    parser.add_argument("--codex-skills", type=Path, default=Path.home() / ".codex" / "skills")
    parser.add_argument("--claude-skills", type=Path, default=Path.home() / ".claude" / "skills")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    src = skill_root()
    targets = [args.codex_skills / "agent-mailbox", args.claude_skills / "agent-mailbox"]
    for dst in targets:
        print(f"{src} -> {dst}")
        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copytree_replace(src, dst)


if __name__ == "__main__":
    main()

