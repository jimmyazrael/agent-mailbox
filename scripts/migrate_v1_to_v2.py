#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from agent_chat import DDL, SCHEMA_VERSION  # noqa: E402
from mailbox_lib import utc_now  # noqa: E402

COPY_TABLES = (
    "rooms",
    "participants",
    "agent_sessions",
    "panes",
    "tui_relay_state",
    "messages",
    "receipts",
    "room_state",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _schema_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_meta"):
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    if not _table_exists(src, table):
        return 0
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return 0
    columns = rows[0].keys()
    names = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    dst.executemany(
        f"INSERT OR REPLACE INTO {table}({names}) VALUES({placeholders})",
        [tuple(row[col] for col in columns) for row in rows],
    )
    return len(rows)


def _legacy_source_hash(row: sqlite3.Row) -> str:
    payload: dict[str, Any] = {
        "id": row["id"],
        "room_id": row["room_id"],
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "kind": row["kind"],
        "status": row["status"],
        "summary": row["summary"],
        "body_text": row["body_text"],
        "body_path": row["body_path"],
        "created_at": row["created_at"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rebuild_message_sources(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    now = utc_now()
    if _table_exists(src, "message_sources"):
        return _copy_table(src, dst, "message_sources")
    rows = src.execute("SELECT * FROM messages WHERE kind='outbox' ORDER BY id").fetchall()
    if not rows:
        return 0
    dst.executemany(
        "INSERT OR REPLACE INTO message_sources(message_id, room_id, source_type, source_path, source_hash, imported_at) "
        "VALUES(?,?,?,?,?,?)",
        [
            (
                row["id"],
                row["room_id"],
                "legacy_migration",
                None,
                _legacy_source_hash(row),
                row["created_at"] or now,
            )
            for row in rows
        ],
    )
    return len(rows)


def migrate_db(source: Path, dest: Path, *, overwrite: bool = False) -> dict[str, object]:
    source = source.resolve()
    dest = dest.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database not found: {source}")
    if source == dest:
        raise ValueError("source and destination must be different files")
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"destination exists: {dest}")
        dest.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(dest) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(str(source))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(dest), isolation_level=None)
    dst.row_factory = sqlite3.Row
    try:
        old_version = _schema_version(src)
        if old_version >= SCHEMA_VERSION:
            raise ValueError(f"source schema version {old_version} is not older than target {SCHEMA_VERSION}")
        dst.execute("PRAGMA foreign_keys=ON")
        dst.executescript(DDL)
        dst.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        copied: dict[str, int] = {}
        dst.execute("BEGIN IMMEDIATE")
        try:
            for table in COPY_TABLES:
                copied[table] = _copy_table(src, dst, table)
            copied["message_sources"] = _rebuild_message_sources(src, dst)
            dst.execute("COMMIT")
        except Exception:
            dst.execute("ROLLBACK")
            raise
        return {
            "source": str(source),
            "dest": str(dest),
            "from_schema_version": old_version,
            "to_schema_version": SCHEMA_VERSION,
            "copied": copied,
        }
    finally:
        src.close()
        dst.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate an agent-mailbox v1 SQLite database to v2.")
    parser.add_argument("source", type=Path, help="Path to the existing v1 agent-chat.sqlite")
    parser.add_argument("dest", type=Path, help="Path for the new v2 agent-chat.sqlite")
    parser.add_argument("--overwrite", action="store_true", help="Replace the destination DB if it already exists")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = migrate_db(args.source, args.dest, overwrite=args.overwrite)
    except Exception as exc:
        if args.format == "json":
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps({"ok": True, **result}, indent=2))
    else:
        print(f"migrated {result['source']} -> {result['dest']} (schema {result['from_schema_version']} -> {result['to_schema_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
