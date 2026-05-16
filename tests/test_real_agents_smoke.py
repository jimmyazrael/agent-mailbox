import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.real_agents
def test_real_agents_one_round_smoke(tmp_path):
    """Opt-in smoke for U1/U2.

    This intentionally launches real Claude/Codex TUIs through WezTerm. The
    conftest marker skips it unless AGENT_MAILBOX_RUN_REAL_SMOKE=1.
    """
    skill_root = Path(__file__).resolve().parent.parent
    mailbox_py = skill_root / "scripts" / "mailbox.py"
    root = tmp_path / "mb"
    rv = subprocess.run(
        [
            sys.executable,
            str(mailbox_py),
            "start",
            "--root",
            str(root),
            "--prefix",
            "smoke",
            "--label",
            "u1u2",
            "--goal",
            "Claude says hello. Codex replies final. Keep responses one sentence.",
            "--project-cwd",
            str(tmp_path),
            "--first-turn",
            "claude",
            "--max-iters",
            "30",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert rv.returncode == 0, rv.stderr or rv.stdout
    out = json.loads(rv.stdout)
    task_id = out["data"]["task_id"]

    from agent_chat import connect_db, room_state_get

    deadline = time.time() + 300
    last_status = None
    last_message_id = 0
    while time.time() < deadline:
        conn = connect_db(root)
        try:
            room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
            last_status = room["status"]
            last_message_id = room["last_message_id"]
            if last_status in {"final", "error", "stopped"}:
                sessions = {
                    row["agent"]: dict(row)
                    for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
                }
                usage = room_state_get(conn, task_id, "usage", default={}) or {}
                break
        finally:
            conn.close()
        time.sleep(5)
    else:
        pytest.fail(f"real-agent smoke timed out; status={last_status!r}, last_message_id={last_message_id}")

    assert last_status == "final"
    assert last_message_id >= 2
    assert sessions["claude"]["session_id"]
    assert sessions["codex"]["discovery_status"] == "discovered"
    assert float(usage.get("known_cost_usd", 0.0)) <= 0.10
