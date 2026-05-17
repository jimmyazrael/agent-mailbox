from pathlib import Path

from agent_chat import add_participant, connect_db, init_db, init_room, send_message, set_pane
from tui_relay_v2 import doorbell_text, first_turn_text, run_once, run_watcher_loop


def _seed(root: Path):
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=root, workspace="agent-mailbox-t1", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    set_pane(conn, "t1", "claude", pane_id=11)
    set_pane(conn, "t1", "codex", pane_id=12)
    return conn


def _outbox_text(author="codex", peer="claude"):
    return (
        "---\n"
        f"from: {author}\n"
        f"to: {peer}\n"
        "status: continue\n"
        "summary: reply\n"
        "---\n\n"
        "Peer body.\n\n"
        "<!-- AGENT-MAILBOX:DONE -->\n"
    )


def test_doorbell_text_points_to_outbox_and_sentinel(tmp_path):
    text = doorbell_text(agent="codex", peer="claude", task_id="t1", root=tmp_path)
    assert "Read the latest reply in the chat monitor pane" in text
    assert str(tmp_path / "t1" / "outbox" / "codex" / "000001.md") in text
    assert "<!-- AGENT-MAILBOX:DONE -->" in text
    assert "Then stop." in text
    assert "from: codex" in text
    assert "to: claude" in text
    assert "status: <continue | blocked | final | error>" in text


def test_first_turn_text_points_to_bootstrap_context_and_outbox(tmp_path):
    text = first_turn_text(agent="claude", peer="codex", task_id="t1", root=tmp_path)
    assert "You have the first turn" in text
    assert "Read the bootstrap context in the chat monitor pane" in text
    assert str(tmp_path / "t1" / "outbox" / "claude" / "000001.md") in text
    assert "from: claude" in text
    assert "to: codex" in text
    assert "status: <continue | blocked | final | error>" in text


def test_run_once_imports_outbox_then_sends_doorbell_to_next_turn(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    path = tmp_path / "t1" / "outbox" / "codex" / "000001.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_outbox_text(), encoding="utf-8")
    calls = []
    monkeypatch.setattr("tui_relay_v2.send_doorbell", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)

    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "doorbell_sent"

    msg = conn.execute("SELECT * FROM messages WHERE room_id='t1'").fetchone()
    assert msg["kind"] == "outbox"
    assert msg["from_agent"] == "codex"
    assert msg["to_agent"] == "claude"
    assert calls[0]["pane_id"] == 11
    assert "chat monitor pane" in calls[0]["text"]
    assert "outbox\\claude\\000001.md" in calls[0]["text"] or "outbox/claude/000001.md" in calls[0]["text"]


def test_run_once_sends_doorbell_to_first_turn_only_once(monkeypatch, tmp_path):
    _seed(tmp_path)
    calls = []
    monkeypatch.setattr("tui_relay_v2.send_doorbell", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "doorbell_sent"
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "idle"
    assert len(calls) == 1


def test_run_once_pauses_on_malformed_outbox(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    path = tmp_path / "t1" / "outbox" / "codex" / "000001.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_outbox_text() + "extra\n", encoding="utf-8")
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "malformed_outbox"
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "malformed_outbox:trailing_content_after_sentinel"


def test_run_once_pauses_on_invalid_outbox_status(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    path = tmp_path / "t1" / "outbox" / "claude" / "000001.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nfrom: claude\nto: codex\nstatus: ready\nsummary: bad\n---\n\nbody\n\n<!-- AGENT-MAILBOX:DONE -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "malformed_outbox"
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "malformed_outbox:invalid_status"


def test_watcher_exits_and_records_fail_closed_result(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    path = tmp_path / "t1" / "outbox" / "claude" / "000001.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nfrom: claude\nto: codex\nstatus: ready\nsummary: bad\n---\n\nbody\n\n<!-- AGENT-MAILBOX:DONE -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    assert run_watcher_loop(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm"), poll_interval_s=0.01, max_iters=5) == "malformed_outbox"
    relay = conn.execute("SELECT paused, pause_reason, watcher_last_result, watcher_finished_at FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "malformed_outbox:invalid_status"
    assert relay["watcher_last_result"] == "malformed_outbox"
    assert relay["watcher_finished_at"]


def test_run_once_respects_db_only_user_inject(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    send_message(
        conn,
        root=tmp_path,
        room_id="t1",
        from_agent="user",
        to_agent="claude",
        kind="inject",
        status="continue",
        summary="guidance",
        body="please adjust",
        increment_round=False,
        next_turn="claude",
    )
    calls = []
    monkeypatch.setattr("tui_relay_v2.send_doorbell", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "doorbell_sent"
    assert len(calls) == 1
    assert not (tmp_path / "t1" / "outbox" / "user").exists()


def test_run_once_pauses_when_completed_session_has_no_outbox(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    conn.execute("INSERT OR REPLACE INTO agent_sessions(room_id, agent, session_id, session_name) VALUES('t1', 'claude', 'claude-1', 's')")
    calls = []
    monkeypatch.setattr("tui_relay_v2.send_doorbell", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    monkeypatch.setattr("tui_relay_v2.MISSING_OUTBOX_GRACE_S", 0.0)
    monkeypatch.setattr("session_logs.find_session_log", lambda **kwargs: tmp_path / "claude.jsonl")
    monkeypatch.setattr(
        "session_logs.latest_completed_turn",
        lambda path, agent: {"path": str(path), "timestamp": "t", "text": "done", "hash": "h1", "mtime": 1},
    )

    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "doorbell_sent"
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "missing_outbox_after_turn"
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "missing_outbox_after_turn:claude"


def test_run_once_marks_session_completion_handled_after_outbox(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    conn.execute("INSERT OR REPLACE INTO agent_sessions(room_id, agent, session_id, session_name) VALUES('t1', 'codex', 'codex-1', 's')")
    path = tmp_path / "t1" / "outbox" / "codex" / "000001.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_outbox_text(), encoding="utf-8")
    monkeypatch.setattr("tui_relay_v2.send_doorbell", lambda **kwargs: True)
    monkeypatch.setattr("tui_relay_v2._pane_alive", lambda *args, **kwargs: True)
    monkeypatch.setattr("tui_relay_v2.MISSING_OUTBOX_GRACE_S", 0.0)
    monkeypatch.setattr("session_logs.find_session_log", lambda **kwargs: tmp_path / "codex.jsonl")
    monkeypatch.setattr(
        "session_logs.latest_completed_turn",
        lambda path, agent: {"path": str(path), "timestamp": "t", "text": "done", "hash": "handled", "mtime": 1},
    )

    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "doorbell_sent"
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "idle"
    relay = conn.execute("SELECT paused FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 0
