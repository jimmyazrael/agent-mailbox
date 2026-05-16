from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def test_init_mailbox_creates_state_and_messages(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    subprocess.run(
        [
            sys.executable,
            str(SKILL / "scripts" / "init_mailbox.py"),
            "--root",
            str(root),
            "--task-id",
            "t1",
            "--goal",
            "Reach consensus",
            "--project-cwd",
            str(tmp_path),
            "--first-turn",
            "codex",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    state = json.loads((root / "t1" / "state.json").read_text(encoding="utf-8"))
    assert state["turn"] == "codex"
    assert state["status"] == "waiting"
    assert state["last_message_id"] == 0
    assert "Reach consensus" in (root / "t1" / "messages.md").read_text(encoding="utf-8")

