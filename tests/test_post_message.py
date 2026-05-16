from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def _init(root: Path, cwd: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(SKILL / "scripts" / "init_mailbox.py"),
            "--root",
            str(root),
            "--task-id",
            "t1",
            "--goal",
            "Test",
            "--project-cwd",
            str(cwd),
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def test_post_message_flips_turn(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    _init(root, tmp_path)
    subprocess.run(
        [
            sys.executable,
            str(SKILL / "scripts" / "post_message.py"),
            "--root",
            str(root),
            "--task-id",
            "t1",
            "--author",
            "codex",
            "--target",
            "claude",
            "--summary",
            "hello",
        ],
        input="Review this.\nMAILBOX_STATUS: continue\n",
        check=True,
        text=True,
        capture_output=True,
    )
    state = json.loads((root / "t1" / "state.json").read_text(encoding="utf-8"))
    assert state["last_message_id"] == 1
    assert state["round"] == 1
    assert state["turn"] == "claude"
    assert state["status"] == "waiting"


def test_post_message_final_stops_task(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    _init(root, tmp_path)
    subprocess.run(
        [
            sys.executable,
            str(SKILL / "scripts" / "post_message.py"),
            "--root",
            str(root),
            "--task-id",
            "t1",
            "--author",
            "codex",
            "--target",
            "claude",
            "--status",
            "final",
            "--summary",
            "done",
        ],
        input="Done.\nMAILBOX_STATUS: final\n",
        check=True,
        text=True,
        capture_output=True,
    )
    state = json.loads((root / "t1" / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "final"
    assert state["turn"] is None

