from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from mailbox_lib import TERMINAL_STATUSES, VALID_MESSAGE_STATUSES, VALID_ROOM_STATUSES, utc_now

SCHEMA_VERSION = 1
INLINE_BODY_THRESHOLD_BYTES = 4096
ALLOWED_ROOM_STATE_KEYS = frozenset({"limits", "usage", "tags", "goal_metadata"})
PROTOCOL_OWNER_RE = re.compile(r"(?im)^[ \t]*Owner[ \t]*:[ \t]*([^\r\n]*)[ \t]*$")

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_room_id TEXT REFERENCES rooms(id),
    purpose TEXT NOT NULL,
    project_cwd TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL,
    turn TEXT,
    blocked_reason TEXT,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    round INTEGER NOT NULL DEFAULT 0,
    exit_condition TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);
CREATE TABLE IF NOT EXISTS participants (
    room_id TEXT NOT NULL REFERENCES rooms(id),
    agent TEXT NOT NULL,
    role TEXT,
    PRIMARY KEY(room_id, agent)
);
CREATE TABLE IF NOT EXISTS agent_sessions (
    room_id TEXT NOT NULL REFERENCES rooms(id),
    agent TEXT NOT NULL,
    session_id TEXT,
    session_name TEXT NOT NULL,
    discovery_marker TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'n/a',
    discovery_scanned_files INTEGER NOT NULL DEFAULT 0,
    discovery_last_attempt_at TEXT,
    PRIMARY KEY(room_id, agent)
);
CREATE TABLE IF NOT EXISTS panes (
    room_id TEXT NOT NULL REFERENCES rooms(id),
    pane_role TEXT NOT NULL,
    pane_id INTEGER,
    bound_at TEXT,
    PRIMARY KEY(room_id, pane_role)
);
CREATE TABLE IF NOT EXISTS tui_relay_state (
    room_id TEXT PRIMARY KEY REFERENCES rooms(id),
    last_triggered_agent TEXT,
    last_triggered_turn TEXT,
    last_triggered_message_id INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT,
    watcher_pid INTEGER,
    watcher_started_at TEXT,
    watcher_host TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    body_text TEXT,
    body_path TEXT,
    blocked_reason TEXT,
    reply_to INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room_id_id ON messages(room_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_room_status ON messages(room_id, status);
CREATE TABLE IF NOT EXISTS receipts (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    agent TEXT NOT NULL,
    read_at TEXT,
    ack_at TEXT,
    PRIMARY KEY(message_id, agent)
);
CREATE TABLE IF NOT EXISTS room_state (
    room_id TEXT NOT NULL REFERENCES rooms(id),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY(room_id, key)
);
"""


def db_path(root: Path) -> Path:
    return root / "agent-chat.sqlite"


def connect_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path(root)), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def init_db(root: Path) -> None:
    conn = connect_db(root)
    try:
        conn.executescript(DDL)
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            existing = int(row[0])
            if existing > SCHEMA_VERSION:
                raise RuntimeError(
                    f"agent-chat.sqlite has newer schema version {existing}; "
                    f"this code only supports {SCHEMA_VERSION}"
                )
            if existing < SCHEMA_VERSION:
                raise RuntimeError(f"unexpected older schema version {existing}; expected {SCHEMA_VERSION}")
    finally:
        conn.close()


def init_room(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    name: str,
    purpose: str,
    project_cwd: Path,
    workspace: str,
    first_turn: str,
    parent_room_id: Optional[str] = None,
    exit_condition: Optional[str] = None,
    status: str = "waiting",
) -> None:
    if status not in VALID_ROOM_STATUSES:
        raise ValueError(f"invalid room status: {status}")
    if parent_room_id is not None and not exit_condition:
        raise ValueError("sub-rooms must declare an exit_condition")
    if conn.execute("SELECT id FROM rooms WHERE id=?", (room_id,)).fetchone():
        raise ValueError(f"room {room_id} already exists")
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO rooms(id, name, parent_room_id, purpose, project_cwd, workspace, "
            "status, turn, blocked_reason, last_message_id, round, exit_condition, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,0,?,?,?)",
            (
                room_id,
                name,
                parent_room_id,
                purpose,
                str(project_cwd),
                workspace,
                status,
                first_turn,
                None,
                exit_condition,
                now,
                now,
            ),
        )
        conn.execute("INSERT INTO tui_relay_state(room_id) VALUES(?)", (room_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def add_participant(conn: sqlite3.Connection, room_id: str, agent: str, *, role: Optional[str] = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO participants(room_id, agent, role) VALUES(?,?,?)",
        (room_id, agent, role),
    )


def set_session_metadata(
    conn: sqlite3.Connection,
    room_id: str,
    agent: str,
    *,
    session_id: Optional[str],
    session_name: str,
    discovery_marker: Optional[str] = None,
    discovery_status: str = "n/a",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO agent_sessions("
        "room_id, agent, session_id, session_name, discovery_marker, discovery_status"
        ") VALUES(?,?,?,?,?,?)",
        (room_id, agent, session_id, session_name, discovery_marker, discovery_status),
    )


def update_codex_discovery(
    conn: sqlite3.Connection,
    room_id: str,
    *,
    session_id: Optional[str],
    status: str,
    scanned_files: int,
    attempted_at: str,
) -> None:
    conn.execute(
        "UPDATE agent_sessions SET session_id=COALESCE(?, session_id), discovery_status=?, "
        "discovery_scanned_files=?, discovery_last_attempt_at=? "
        "WHERE room_id=? AND agent='codex'",
        (session_id, status, scanned_files, attempted_at, room_id),
    )


def set_pane(conn: sqlite3.Connection, room_id: str, pane_role: str, *, pane_id: Optional[int]) -> None:
    conn.execute(
        "INSERT INTO panes(room_id, pane_role, pane_id, bound_at) VALUES(?,?,?,?) "
        "ON CONFLICT(room_id, pane_role) DO UPDATE SET pane_id=excluded.pane_id, bound_at=excluded.bound_at",
        (room_id, pane_role, pane_id, utc_now()),
    )


def _write_artifact(root: Path, room_id: str, message_id: int, body: str) -> str:
    artifact_dir = root / room_id / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"msg-{message_id:06d}.md"
    path.write_text(body, encoding="utf-8", newline="\n")
    return f"artifacts/msg-{message_id:06d}.md"


def owner_from_protocol(body: str) -> Optional[str]:
    match = PROTOCOL_OWNER_RE.search(body or "")
    if not match:
        return None
    owner = match.group(1).strip().lower()
    if owner in {"", "none", "n/a", "na", "-"}:
        return None
    return owner


def _next_turn_from_continue(
    conn: sqlite3.Connection,
    *,
    room_id: str,
    from_agent: str,
    to_agent: Optional[str],
    body: str,
    next_turn: Optional[str],
) -> Optional[str]:
    if next_turn is not None:
        return next_turn
    owner = owner_from_protocol(body)
    if owner:
        participants = {row["agent"] for row in conn.execute("SELECT agent FROM participants WHERE room_id=?", (room_id,)).fetchall()}
        if owner in participants:
            return owner
    if to_agent == from_agent:
        return from_agent
    return to_agent


def _participant_names(conn: sqlite3.Connection, room_id: str) -> set[str]:
    return {row["agent"] for row in conn.execute("SELECT agent FROM participants WHERE room_id=?", (room_id,)).fetchall()}


def _max_rounds(conn: sqlite3.Connection, room_id: str) -> Optional[int]:
    row = conn.execute("SELECT value_json FROM room_state WHERE room_id=? AND key='limits'", (room_id,)).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    max_rounds = value.get("max_rounds") if isinstance(value, dict) else None
    return int(max_rounds) if isinstance(max_rounds, int) and max_rounds > 0 else None


def _mark_room_error(conn: sqlite3.Connection, *, room_id: str, reason: str, now: str) -> None:
    conn.execute(
        "UPDATE rooms SET turn=NULL, status='error', blocked_reason=?, updated_at=? WHERE id=?",
        (reason, now, room_id),
    )


def send_message(
    conn: sqlite3.Connection,
    *,
    root: Path,
    room_id: str,
    from_agent: str,
    to_agent: Optional[str],
    kind: str,
    status: str,
    summary: str,
    body: str,
    blocked_reason: Optional[str] = None,
    next_turn: Optional[str] = None,
    increment_round: bool = True,
    reply_to: Optional[int] = None,
) -> Dict[str, Any]:
    if status not in VALID_MESSAGE_STATUSES:
        raise ValueError(f"invalid message status: {status}")
    now = utc_now()
    inline = len((body or "").encode("utf-8")) <= INLINE_BODY_THRESHOLD_BYTES
    artifact_path: Optional[Path] = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        room = conn.execute("SELECT round FROM rooms WHERE id=?", (room_id,)).fetchone()
        if room is None:
            raise ValueError(f"room not found: {room_id}")
        new_turn: Optional[str] = None
        if status == "continue":
            if increment_round:
                max_rounds = _max_rounds(conn, room_id)
                if max_rounds is not None and int(room["round"]) + 1 > max_rounds:
                    reason = f"max_rounds_exceeded:{max_rounds}"
                    _mark_room_error(conn, room_id=room_id, reason=reason, now=now)
                    conn.execute("COMMIT")
                    raise RuntimeError(reason)
            new_turn = _next_turn_from_continue(
                conn,
                room_id=room_id,
                from_agent=from_agent,
                to_agent=to_agent,
                body=body,
                next_turn=next_turn,
            )
            if new_turn is not None and new_turn not in _participant_names(conn, room_id):
                reason = f"invalid_turn_target:{new_turn}"
                _mark_room_error(conn, room_id=room_id, reason=reason, now=now)
                conn.execute("COMMIT")
                raise ValueError(reason)
        cur = conn.execute(
            "INSERT INTO messages(room_id, from_agent, to_agent, kind, status, summary, "
            "body_text, body_path, blocked_reason, reply_to, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                room_id,
                from_agent,
                to_agent,
                kind,
                status,
                summary,
                body if inline else None,
                None,
                blocked_reason,
                reply_to,
                now,
            ),
        )
        message_id = int(cur.lastrowid)
        rel_path = None
        if not inline:
            rel_path = _write_artifact(root, room_id, message_id, body)
            artifact_path = root / room_id / rel_path
            conn.execute("UPDATE messages SET body_path=? WHERE id=?", (rel_path, message_id))
        if status == "continue":
            conn.execute(
                "UPDATE rooms SET turn=?, status='waiting', last_message_id=?, round=round+?, "
                "blocked_reason=NULL, updated_at=? WHERE id=?",
                (new_turn, message_id, 1 if increment_round else 0, now, room_id),
            )
        elif status == "blocked":
            conn.execute(
                "UPDATE rooms SET turn=?, status='blocked', last_message_id=?, blocked_reason=?, updated_at=? "
                "WHERE id=?",
                (from_agent, message_id, blocked_reason or "unspecified", now, room_id),
            )
        else:
            conn.execute(
                "UPDATE rooms SET turn=NULL, status=?, last_message_id=?, blocked_reason=NULL, updated_at=? "
                "WHERE id=?",
                (status, message_id, now, room_id),
            )
        conn.execute("COMMIT")
        return {"message_id": message_id, "created_at": now, "body_inline": inline, "body_path": rel_path}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            if "no transaction is active" not in str(exc).lower():
                raise
        if artifact_path is not None:
            try:
                artifact_path.unlink()
            except OSError:
                pass
        raise


def get_message_body(root: Path, message_id: int) -> str:
    conn = connect_db(root)
    try:
        row = conn.execute("SELECT room_id, body_text, body_path FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(f"message {message_id} not found")
        if row["body_text"] is not None:
            return row["body_text"]
        return (root / row["room_id"] / row["body_path"]).read_text(encoding="utf-8")
    finally:
        conn.close()


def peek_latest(conn: sqlite3.Connection, room_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT 1", (room_id,)).fetchone()
    return dict(row) if row else None


def read_unread(conn: sqlite3.Connection, room_id: str, *, agent: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.* FROM messages m "
        "LEFT JOIN receipts r ON r.message_id=m.id AND r.agent=? "
        "WHERE m.room_id=? "
        "AND (m.to_agent IS NULL OR m.to_agent=? OR m.to_agent='broadcast') "
        "AND m.from_agent != ? "
        "AND r.ack_at IS NULL "
        "ORDER BY m.id ASC",
        (agent, room_id, agent, agent),
    ).fetchall()
    return [dict(row) for row in rows]


def ack_message(conn: sqlite3.Connection, *, message_id: int, agent: str) -> None:
    now = utc_now()
    conn.execute(
        "INSERT INTO receipts(message_id, agent, read_at, ack_at) VALUES(?,?,?,?) "
        "ON CONFLICT(message_id, agent) DO UPDATE SET ack_at=excluded.ack_at, "
        "read_at=COALESCE(receipts.read_at, excluded.read_at)",
        (message_id, agent, now, now),
    )


def room_state_set(conn: sqlite3.Connection, room_id: str, key: str, value: Any) -> None:
    if key not in ALLOWED_ROOM_STATE_KEYS:
        raise ValueError(f"room_state key {key!r} not in allowed keys {sorted(ALLOWED_ROOM_STATE_KEYS)}")
    conn.execute(
        "INSERT INTO room_state(room_id, key, value_json) VALUES(?,?,?) "
        "ON CONFLICT(room_id, key) DO UPDATE SET value_json=excluded.value_json",
        (room_id, key, json.dumps(value, sort_keys=True)),
    )


def room_state_get(conn: sqlite3.Connection, room_id: str, key: str, *, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM room_state WHERE room_id=? AND key=?", (room_id, key)).fetchone()
    return json.loads(row["value_json"]) if row else default


def resolve_task_id(conn: sqlite3.Connection, query: str) -> str:
    ids = [row["id"] for row in conn.execute("SELECT id FROM rooms ORDER BY id").fetchall()]
    if query in ids:
        return query
    matches = [task_id for task_id in ids if task_id.startswith(query)]
    if not matches:
        raise ValueError(f"no task id matches: {query}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous task id query {query!r}: {matches}")
    return matches[0]


def list_rooms(conn: sqlite3.Connection, *, active_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT id, name, status, turn, round, last_message_id, updated_at FROM rooms"
    params: tuple[Any, ...] = ()
    if active_only:
        sql += " WHERE status NOT IN (?,?,?)"
        params = tuple(TERMINAL_STATUSES)
    sql += " ORDER BY updated_at DESC"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def export_transcript_md(conn: sqlite3.Connection, *, root: Path, room_id: str) -> str:
    room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if room is None:
        raise KeyError(f"room {room_id} not found")
    lines = [
        f"# Agent Mailbox Transcript: {room_id}",
        "",
        f"Goal: {room['purpose']}",
        f"Status: {room['status']}",
        f"Created: {room['created_at']}",
        f"Last update: {room['updated_at']}",
        "",
    ]
    rows = conn.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id ASC", (room_id,)).fetchall()
    for msg in rows:
        lines.extend(
            [
                "---",
                "",
                f"## MSG {msg['id']} - {msg['from_agent']} -> {msg['to_agent'] or '(broadcast)'}",
                "",
                f"kind: {msg['kind']}",
                f"status: {msg['status']}",
                f"timestamp: {msg['created_at']}",
                f"summary: {(msg['summary'] or '').strip()}",
            ]
        )
        if msg["blocked_reason"]:
            lines.append(f"blocked_reason: {msg['blocked_reason']}")
        lines.extend(["", "### Content", ""])
        if msg["body_text"] is not None:
            lines.append(msg["body_text"])
        elif msg["body_path"]:
            artifact = root / room_id / msg["body_path"]
            try:
                lines.append(artifact.read_text(encoding="utf-8"))
            except FileNotFoundError:
                lines.append(f"[artifact missing: {msg['body_path']}]")
        lines.append("")
    return "\n".join(lines)
