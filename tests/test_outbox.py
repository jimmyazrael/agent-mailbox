from pathlib import Path

import pytest

from agent_chat import add_participant, connect_db, init_db, init_room
from outbox import OutboxError, import_outbox_messages, next_outbox_path, parse_outbox_message


def _seed(root: Path):
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=root, workspace="agent-mailbox-t1", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    return conn


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _message(**overrides) -> str:
    fields = {
        "from": "claude",
        "to": "codex",
        "status": "continue",
        "summary": "review",
    }
    fields.update(overrides)
    header = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{header}\n---\n\nBody line.\n\n<!-- AGENT-MAILBOX:DONE -->\n"


def test_next_outbox_path_is_deterministic_and_ignores_pending_and_draft(tmp_path):
    _write(tmp_path / "t1" / "outbox" / "claude" / "000001.md", _message())
    _write(tmp_path / "t1" / "outbox" / "claude" / "000999.pending.md", "draft")
    _write(tmp_path / "t1" / "outbox" / "claude" / "000888.draft.md", "draft")
    assert next_outbox_path(tmp_path, "t1", "claude") == tmp_path / "t1" / "outbox" / "claude" / "000002.md"


def test_parse_outbox_message_accepts_simple_frontmatter_without_yaml(tmp_path):
    path = tmp_path / "msg.md"
    _write(path, _message())
    msg = parse_outbox_message(path)
    assert msg.from_agent == "claude"
    assert msg.to_agent == "codex"
    assert msg.status == "continue"
    assert msg.summary == "review"
    assert msg.body == "Body line."
    assert len(msg.source_hash) == 64


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("from: claude\n\nbody\n<!-- AGENT-MAILBOX:DONE -->\n", "missing_frontmatter_open"),
        ("---\nfrom claude\n---\nbody\n<!-- AGENT-MAILBOX:DONE -->\n", "malformed_frontmatter_header"),
        ("---\nfrom: claude\n\n---\nbody\n<!-- AGENT-MAILBOX:DONE -->\n", "malformed_frontmatter_blank_line"),
        ("---\nfrom: claude\nbody: nope\n<!-- AGENT-MAILBOX:DONE -->\n", "malformed_frontmatter_header"),
        ("---\nfrom: claude\n---\nbody\n", "missing_done_sentinel"),
        (_message() + "<!-- AGENT-MAILBOX:DONE -->\n", "multiple_done_sentinels"),
        (_message() + "extra\n", "trailing_content_after_sentinel"),
        (_message(**{"from": "nobody"}), "invalid_from_agent"),
        (_message(**{"to": "nobody"}), "invalid_to_agent"),
        (_message(**{"from": "claude", "to": "claude"}), "self_addressed_message"),
        (_message(**{"status": "maybe"}), "invalid_status"),
        (_message(summary=""), "missing_summary"),
        ("---\nfrom: claude\nto: codex\nstatus: continue\nsummary: s\n---\n\n<!-- AGENT-MAILBOX:DONE -->\n", "missing_body"),
    ],
)
def test_parse_outbox_message_rejects_malformed_files(tmp_path, text, reason):
    path = tmp_path / "bad.md"
    _write(path, text)
    with pytest.raises(OutboxError) as exc:
        parse_outbox_message(path)
    assert exc.value.reason == reason


def test_import_outbox_messages_writes_messages_and_sources_once(tmp_path):
    conn = _seed(tmp_path)
    path = tmp_path / "t1" / "outbox" / "claude" / "000001.md"
    _write(path, _message())

    first = import_outbox_messages(conn, root=tmp_path, room_id="t1")
    second = import_outbox_messages(conn, root=tmp_path, room_id="t1")

    assert len(first) == 1
    assert second == []
    msg = conn.execute("SELECT * FROM messages WHERE room_id='t1'").fetchone()
    assert msg["from_agent"] == "claude"
    assert msg["to_agent"] == "codex"
    assert msg["kind"] == "outbox"
    assert msg["body_text"] == "Body line."
    source = conn.execute("SELECT * FROM message_sources WHERE message_id=?", (msg["id"],)).fetchone()
    assert source["source_type"] == "outbox"
    assert source["source_path"] == "t1\\outbox\\claude\\000001.md" or source["source_path"] == "t1/outbox/claude/000001.md"


def test_import_outbox_messages_ignores_pending_and_draft(tmp_path):
    conn = _seed(tmp_path)
    _write(tmp_path / "t1" / "outbox" / "claude" / "000001.pending.md", _message())
    _write(tmp_path / "t1" / "outbox" / "claude" / "000002.draft.md", _message())
    assert import_outbox_messages(conn, root=tmp_path, room_id="t1") == []


def test_import_outbox_messages_rejects_changed_imported_file(tmp_path):
    conn = _seed(tmp_path)
    path = tmp_path / "t1" / "outbox" / "claude" / "000001.md"
    _write(path, _message(summary="first"))
    assert len(import_outbox_messages(conn, root=tmp_path, room_id="t1")) == 1
    _write(path, _message(summary="changed"))
    with pytest.raises(OutboxError) as exc:
        import_outbox_messages(conn, root=tmp_path, room_id="t1")
    assert exc.value.reason == "outbox_file_changed_after_import"
