import sqlite3
from pathlib import Path

import pytest

from agent_chat import (
    ack_message,
    add_participant,
    connect_db,
    export_transcript_md,
    get_message_body,
    init_db,
    init_room,
    list_rooms,
    owner_from_protocol,
    peek_latest,
    read_unread,
    resolve_task_id,
    room_state_get,
    room_state_set,
    schema_version,
    send_message,
    set_pane,
    set_session_metadata,
)


def _setup(root: Path):
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=root, workspace="agent-mailbox-t1", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    return conn


def test_init_db_creates_all_tables(tmp_path):
    init_db(tmp_path)
    conn = connect_db(tmp_path)
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "schema_meta",
        "rooms",
        "participants",
        "agent_sessions",
        "panes",
        "tui_relay_state",
        "messages",
        "receipts",
        "room_state",
    }.issubset(tables)
    assert schema_version(conn) == 1


def test_init_db_rejects_newer_version(tmp_path):
    init_db(tmp_path)
    conn = connect_db(tmp_path)
    conn.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(RuntimeError, match="newer schema version"):
        init_db(tmp_path)


def test_room_identity_and_panes(tmp_path):
    conn = _setup(tmp_path)
    set_session_metadata(conn, "t1", "claude", session_id="c1", session_name="agent-mailbox-t1-claude")
    set_session_metadata(
        conn,
        "t1",
        "codex",
        session_id=None,
        session_name="agent-mailbox-t1-codex",
        discovery_marker="AGENT_MAILBOX_TASK_ID=t1",
        discovery_status="pending",
    )
    set_pane(conn, "t1", "claude", pane_id=11)
    set_pane(conn, "t1", "claude", pane_id=99)
    assert conn.execute("SELECT pane_id FROM panes WHERE room_id='t1' AND pane_role='claude'").fetchone()["pane_id"] == 99


def test_subroom_requires_exit_condition(tmp_path):
    conn = _setup(tmp_path)
    with pytest.raises(ValueError, match="exit_condition"):
        init_room(conn, room_id="child", name="C", purpose="sub", project_cwd=tmp_path, workspace="ws", first_turn="claude", parent_room_id="t1")


def test_send_message_inline_artifact_and_body_read(tmp_path):
    conn = _setup(tmp_path)
    rv1 = send_message(conn, root=tmp_path, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="continue", summary="small", body="hi")
    rv2 = send_message(conn, root=tmp_path, room_id="t1", from_agent="codex", to_agent="claude", kind="message", status="continue", summary="big", body="x" * 5000)
    assert rv1["body_inline"] is True
    assert rv2["body_inline"] is False
    assert get_message_body(tmp_path, rv1["message_id"]) == "hi"
    assert get_message_body(tmp_path, rv2["message_id"]) == "x" * 5000
    room = conn.execute("SELECT turn, round, last_message_id FROM rooms WHERE id='t1'").fetchone()
    assert room["turn"] == "claude"
    assert room["round"] == 2
    assert room["last_message_id"] == 2


def test_continue_routes_to_protocol_owner_even_when_reviewer_is_recipient(tmp_path):
    conn = _setup(tmp_path)
    body = "\n".join(
        [
            "Mode: EXECUTE",
            "Coordinator: claude",
            "Owner: codex",
            "Reviewer: claude",
            "Next action: Codex implements the change.",
            "Done when: tests pass.",
            "",
            "I will implement this.",
        ]
    )
    send_message(
        conn,
        root=tmp_path,
        room_id="t1",
        from_agent="codex",
        to_agent="claude",
        kind="message",
        status="continue",
        summary="codex owns implementation",
        body=body,
    )
    room = conn.execute("SELECT turn FROM rooms WHERE id='t1'").fetchone()
    assert room["turn"] == "codex"


def test_continue_ignores_non_participant_protocol_owner(tmp_path):
    conn = _setup(tmp_path)
    body = "\n".join(
        [
            "Mode: EXECUTE",
            "Coordinator: claude",
            "Owner: buildbot",
            "Reviewer: claude",
            "Next action: Buildbot does the impossible.",
            "Done when: never.",
        ]
    )
    send_message(
        conn,
        root=tmp_path,
        room_id="t1",
        from_agent="codex",
        to_agent="claude",
        kind="message",
        status="continue",
        summary="unknown owner",
        body=body,
    )
    room = conn.execute("SELECT turn FROM rooms WHERE id='t1'").fetchone()
    assert room["turn"] == "claude"


def test_send_message_rejects_invalid_turn_target_and_marks_error(tmp_path):
    conn = _setup(tmp_path)
    with pytest.raises(ValueError, match="invalid_turn_target:buildbot"):
        send_message(
            conn,
            root=tmp_path,
            room_id="t1",
            from_agent="claude",
            to_agent="codex",
            kind="message",
            status="continue",
            summary="bad next turn",
            body="body",
            next_turn="buildbot",
        )
    room = conn.execute("SELECT status, turn, blocked_reason, last_message_id FROM rooms WHERE id='t1'").fetchone()
    assert dict(room) == {
        "status": "error",
        "turn": None,
        "blocked_reason": "invalid_turn_target:buildbot",
        "last_message_id": 0,
    }
    assert conn.execute("SELECT COUNT(*) AS n FROM messages WHERE room_id='t1'").fetchone()["n"] == 0


def test_send_message_enforces_max_rounds_and_marks_error(tmp_path):
    conn = _setup(tmp_path)
    room_state_set(conn, "t1", "limits", {"max_rounds": 1})
    send_message(
        conn,
        root=tmp_path,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="message",
        status="continue",
        summary="round 1",
        body="body",
    )
    with pytest.raises(RuntimeError, match="max_rounds_exceeded:1"):
        send_message(
            conn,
            root=tmp_path,
            room_id="t1",
            from_agent="codex",
            to_agent="claude",
            kind="message",
            status="continue",
            summary="round 2",
            body="body",
        )
    room = conn.execute("SELECT status, turn, blocked_reason, round, last_message_id FROM rooms WHERE id='t1'").fetchone()
    assert dict(room) == {
        "status": "error",
        "turn": None,
        "blocked_reason": "max_rounds_exceeded:1",
        "round": 1,
        "last_message_id": 1,
    }
    assert conn.execute("SELECT COUNT(*) AS n FROM messages WHERE room_id='t1'").fetchone()["n"] == 1


def test_owner_from_protocol_handles_none_and_case():
    assert owner_from_protocol("Mode: EXECUTE\nOwner: CoDeX\n") == "codex"
    assert owner_from_protocol("Mode: DONE\nOwner: none\n") is None
    assert owner_from_protocol("no owner") is None


def test_send_message_rolls_back_artifact_on_db_failure(tmp_path, monkeypatch):
    conn = _setup(tmp_path)
    real_execute = conn.execute

    class ConnProxy:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *params):
            if isinstance(sql, str) and sql.startswith("UPDATE rooms"):
                raise sqlite3.OperationalError("simulated")
            return real_execute(sql, *params)

    with pytest.raises(sqlite3.OperationalError):
        send_message(ConnProxy(conn), root=tmp_path, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="continue", summary="x", body="x" * 5000)
    artifact_dir = tmp_path / "t1" / "artifacts"
    assert not artifact_dir.exists() or list(artifact_dir.iterdir()) == []


def test_read_unread_filters_recipient_and_ack(tmp_path):
    conn = _setup(tmp_path)
    send_message(conn, root=tmp_path, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="continue", summary="m1", body="b1")
    rv = send_message(conn, root=tmp_path, room_id="t1", from_agent="user", to_agent="codex", kind="inject", status="continue", summary="hint", body="b2", next_turn="codex", increment_round=False)
    assert [m["id"] for m in read_unread(conn, "t1", agent="claude")] == []
    assert rv["message_id"] in [m["id"] for m in read_unread(conn, "t1", agent="codex")]
    ack_message(conn, message_id=rv["message_id"], agent="codex")
    assert rv["message_id"] not in [m["id"] for m in read_unread(conn, "t1", agent="codex")]


def test_room_state_is_narrow_eav(tmp_path):
    conn = _setup(tmp_path)
    room_state_set(conn, "t1", "limits", {"max_rounds": 30})
    assert room_state_get(conn, "t1", "limits") == {"max_rounds": 30}
    with pytest.raises(ValueError):
        room_state_set(conn, "t1", "wezterm", {})


def test_resolve_list_and_export(tmp_path):
    conn = _setup(tmp_path)
    init_room(conn, room_id="spc-aaa", name="A", purpose="p", project_cwd=tmp_path, workspace="ws", first_turn="claude")
    assert resolve_task_id(conn, "spc-a") == "spc-aaa"
    conn.execute("UPDATE rooms SET status='final' WHERE id='spc-aaa'")
    assert "spc-aaa" not in [r["id"] for r in list_rooms(conn, active_only=True)]
    send_message(conn, root=tmp_path, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="final", summary="done", body="ok")
    md = export_transcript_md(conn, root=tmp_path, room_id="t1")
    assert "# Agent Mailbox Transcript: t1" in md
    assert "## MSG 1 - claude -> codex" in md
    assert "status: final" in md


def test_transcript_export_is_one_way_and_workspace_immutable(tmp_path):
    conn = _setup(tmp_path)
    send_message(conn, root=tmp_path, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="continue", summary="m1", body="b1")
    transcript = tmp_path / "t1" / "transcript.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(export_transcript_md(conn, root=tmp_path, room_id="t1"), encoding="utf-8")
    transcript.unlink()
    assert peek_latest(conn, "t1")["summary"] == "m1"
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    assert not [py for py in scripts.glob("*.py") if "UPDATE rooms SET workspace" in py.read_text(encoding="utf-8")]
    forbidden = ("import_transcript", "parse_transcript", "load_transcript", "read_transcript_back", "transcript_to_state", "transcript_to_messages")
    suspects = []
    for py in scripts.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        suspects.extend((py.name, name) for name in forbidden if f"def {name}" in text)
    assert suspects == []
