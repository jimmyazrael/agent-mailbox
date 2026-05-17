from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def copytree_replace(src: Path, dst: Path, *, force: bool) -> None:
    if dst.exists():
        if not force:
            raise FileExistsError(f"{dst} already exists; pass --force to overwrite")
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "*.pyc")
    shutil.copytree(src, dst, ignore=ignore)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install agent-mailbox skill for Codex and Claude Code")
    parser.add_argument("--codex-skills", type=Path, default=Path.home() / ".codex" / "skills")
    parser.add_argument("--claude-skills", type=Path, default=Path.home() / ".claude" / "skills")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    src = skill_root()
    targets = [args.codex_skills / "agent-mailbox", args.claude_skills / "agent-mailbox"]
    for dst in targets:
        print(f"{src} -> {dst}")
        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copytree_replace(src, dst, force=args.force)


if __name__ == "__main__":
    main()
