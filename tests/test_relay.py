from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def test_mock_relay_reaches_final(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    responses = tmp_path / "responses.json"
    responses.write_text(
        json.dumps(
            {
                "codex": ["Codex starts and asks Claude to review.\n\nMAILBOX_STATUS: continue"],
                "claude": ["Claude agrees no issues remain.\n\nMAILBOX_STATUS: final"],
            }
        ),
        encoding="utf-8",
    )
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
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(SKILL / "scripts" / "mailbox_relay.py"),
            "--root",
            str(root),
            "--task-id",
            "t1",
            "--backend",
            "mock",
            "--mock-responses",
            str(responses),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    state = json.loads((root / "t1" / "state.json").read_text(encoding="utf-8"))
    messages = (root / "t1" / "messages.md").read_text(encoding="utf-8")
    assert state["status"] == "final"
    assert state["last_message_id"] == 2
    assert "Codex starts" in messages
    assert "Claude agrees" in messages


def test_peer_message_cdata_is_safed() -> None:
    sys.path.insert(0, str(SKILL / "scripts"))
    from mailbox_common import cdata_safe

    assert "]]>" not in cdata_safe("bad ]]> marker")

