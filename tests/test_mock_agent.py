import subprocess
import sys
from pathlib import Path

from agent_chat import add_participant, connect_db, init_db, init_room

SKILL_ROOT = Path(__file__).resolve().parent.parent
MOCK = SKILL_ROOT / "scripts" / "mock_agent.py"


def test_mock_agent_posts_via_sqlite_path(tmp_path):
    init_db(tmp_path)
    conn = connect_db(tmp_path)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="agent-mailbox-t1", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    conn.close()
    rv = subprocess.run(
        [sys.executable, str(MOCK), "--root", str(tmp_path), "--task-id", "t1", "--agent", "claude", "--peer", "codex", "--mode", "continue"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 0, rv.stderr
    conn = connect_db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE room_id='t1'").fetchone()[0] == 1
    assert conn.execute("SELECT turn FROM rooms WHERE id='t1'").fetchone()["turn"] == "codex"
