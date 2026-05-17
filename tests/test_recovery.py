import json
import subprocess
import sys
from pathlib import Path

from agent_chat import add_participant, connect_db, init_db, init_room, set_pane, set_session_metadata

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX = SKILL_ROOT / "scripts" / "mailbox.py"


def _make_task(root: Path, codex_session_id: str | None = "codex-known"):
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=root, workspace="agent-mailbox-t1", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    set_session_metadata(conn, "t1", "claude", session_id="claude-known", session_name="agent-mailbox-t1-claude")
    set_session_metadata(conn, "t1", "codex", session_id=codex_session_id, session_name="agent-mailbox-t1-codex", discovery_status="discovered" if codex_session_id else "failed")
    set_pane(conn, "t1", "claude", pane_id=11)
    set_pane(conn, "t1", "codex", pane_id=12)
    conn.close()


def test_resume_refuses_missing_codex_session(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    _make_task(root, codex_session_id=None)
    rv = subprocess.run(
        [sys.executable, str(MAILBOX), "resume", "--root", str(root), "--task-id", "t1", "--format", "json"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 2
    assert "missing codex session_id" in json.loads(rv.stdout)["error"]


def test_resume_recreates_missing_codex_pane(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    _make_task(root)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: wez)

    def fake_run(argv, **kwargs):
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, '[{"pane_id":11,"workspace":"agent-mailbox-t1"}]', "")
        if "split-pane" in argv:
            return subprocess.CompletedProcess(argv, 0, "99\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["resume", "--root", str(root), "--task-id", "t1", "--format", "json"])
    assert mailbox_cli.cmd_resume(args) == 0
    conn = connect_db(root)
    assert conn.execute("SELECT pane_id FROM panes WHERE room_id='t1' AND pane_role='codex'").fetchone()["pane_id"] == 99


def test_resume_preserves_conversation_state(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    _make_task(root)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: wez)
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '[{"pane_id":11,"workspace":"agent-mailbox-t1"},{"pane_id":12,"workspace":"agent-mailbox-t1"}]' if "list" in argv else "", ""))
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["resume", "--root", str(root), "--task-id", "t1", "--format", "json"])
    mailbox_cli.cmd_resume(args)
    conn = connect_db(root)
    room = conn.execute("SELECT turn, round, last_message_id FROM rooms WHERE id='t1'").fetchone()
    assert dict(room) == {"turn": "claude", "round": 0, "last_message_id": 0}


def test_resume_keeps_relay_paused_when_startup_not_ready(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    _make_task(root)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: wez)
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            '[{"pane_id":11,"workspace":"agent-mailbox-t1"},{"pane_id":12,"workspace":"agent-mailbox-t1"}]' if "list" in argv else "",
            "",
        ),
    )
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": False,
            "visible": True,
            "agents": {"claude": {"state": "ready"}, "codex": {"state": "update_prompt"}},
        },
    )
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["resume", "--root", str(root), "--task-id", "t1", "--format", "json"])
    assert mailbox_cli.cmd_resume(args) == 0
    conn = connect_db(root)
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "startup_not_ready:claude=ready,codex=update_prompt"


def test_repair_restart_agent_applies_startup_gate(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    _make_task(root)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: wez)
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": False,
            "visible": True,
            "agents": {"claude": {"state": "shell"}, "codex": {"state": "ready"}},
        },
    )
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["repair", "--root", str(root), "--task-id", "t1", "--restart-agent", "claude", "--format", "json"])
    assert mailbox_cli.cmd_repair(args) == 0
    conn = connect_db(root)
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "startup_not_ready:claude=shell,codex=ready"
