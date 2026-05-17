from pathlib import Path

from agent_chat import add_participant, connect_db, init_db, init_room, send_message, set_pane
from tui_relay import run_once, trigger_text


def _seed(root: Path):
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=root, workspace="agent-mailbox-t1", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    set_pane(conn, "t1", "claude", pane_id=11)
    set_pane(conn, "t1", "codex", pane_id=12)
    return conn


def test_run_once_triggers_only_once(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    send_message(conn, root=tmp_path, room_id="t1", from_agent="codex", to_agent="claude", kind="message", status="continue", summary="go", body="body")
    calls = []
    monkeypatch.setattr("tui_relay.send_trigger", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "triggered"
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "idle"
    assert len(calls) == 1
    assert calls[0]["root"] == tmp_path
    assert calls[0]["first_turn"] is False


def test_run_once_first_turn_trigger_includes_first_turn_flag(monkeypatch, tmp_path):
    _seed(tmp_path)
    calls = []
    monkeypatch.setattr("tui_relay.send_trigger", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "triggered"
    assert calls[0]["first_turn"] is True


def test_run_once_first_turn_flag_after_context_bootstrap(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    send_message(
        conn,
        root=tmp_path,
        room_id="t1",
        from_agent="user",
        to_agent="broadcast",
        kind="system",
        status="continue",
        summary="bootstrap context",
        body="read this before starting",
        next_turn="claude",
        increment_round=False,
    )
    calls = []
    monkeypatch.setattr("tui_relay.send_trigger", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr("tui_relay._pane_alive", lambda *args, **kwargs: True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "triggered"
    assert calls[0]["first_turn"] is True


def test_trigger_text_includes_literal_discovery_marker(tmp_path):
    text = trigger_text(agent="codex", peer="claude", task_id="t1", root=tmp_path)
    assert "AGENT_MAILBOX_TASK_ID=t1" in text
    assert "--root" in text
    assert "--task-id \"t1\"" in text
    assert "Non-terminal posts need Mode plus Next action or Blocked on" in text


def test_run_once_pauses_when_pane_missing(tmp_path):
    conn = _seed(tmp_path)
    conn.execute("DELETE FROM panes WHERE room_id='t1' AND pane_role='claude'")
    send_message(conn, root=tmp_path, room_id="t1", from_agent="codex", to_agent="claude", kind="message", status="continue", summary="go", body="body")
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "missing_pane"
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "pane_id_missing:claude"


def test_run_once_no_trigger_when_blocked(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    send_message(
        conn,
        root=tmp_path,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="message",
        status="blocked",
        summary="blocked",
        body="need user input",
        blocked_reason="need user input",
    )
    calls = []
    monkeypatch.setattr("tui_relay.send_trigger", lambda **kwargs: calls.append(kwargs) or True)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "blocked"
    assert calls == []


def test_run_once_pauses_when_bound_pane_not_live(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    send_message(conn, root=tmp_path, room_id="t1", from_agent="codex", to_agent="claude", kind="message", status="continue", summary="go", body="body")
    monkeypatch.setattr("tui_relay._pane_alive", lambda *args, **kwargs: False)
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "panes_lost"
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id='t1'").fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "panes_lost:claude"


def test_run_once_exits_on_terminal(tmp_path):
    conn = _seed(tmp_path)
    send_message(conn, root=tmp_path, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="final", summary="done", body="done")
    assert run_once(root=tmp_path, task_id="t1", wezterm_exe=Path("wezterm")) == "terminal"
