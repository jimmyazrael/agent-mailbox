from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent_chat import has_message_source, message_source_hash_for_path, send_message

SENTINEL = "<!-- AGENT-MAILBOX:DONE -->"
VALID_AGENTS = {"claude", "codex"}
VALID_STATUSES = {"continue", "blocked", "final", "error"}
HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
SENTINEL_LINE_RE = re.compile(rf"(?m)^[ \t]*{re.escape(SENTINEL)}[ \t]*$")


class OutboxError(ValueError):
    def __init__(self, reason: str, path: Path | None = None):
        super().__init__(f"{reason}: {path}" if path else reason)
        self.reason = reason
        self.path = path


@dataclass(frozen=True)
class OutboxMessage:
    path: Path
    from_agent: str
    to_agent: str
    status: str
    summary: str
    body: str
    source_hash: str
    blocked_reason: str | None = None


def outbox_root(root: Path, room_id: str) -> Path:
    return root / room_id / "outbox"


def next_outbox_path(root: Path, room_id: str, agent: str) -> Path:
    if agent not in VALID_AGENTS:
        raise ValueError(f"unknown outbox agent: {agent}")
    folder = outbox_root(root, room_id) / agent
    folder.mkdir(parents=True, exist_ok=True)
    used = []
    for path in folder.glob("*.md"):
        if path.name.endswith(".pending.md") or path.name.endswith(".draft.md"):
            continue
        if path.stem.isdigit():
            used.append(int(path.stem))
    return folder / f"{(max(used) if used else 0) + 1:06d}.md"


def _parse_header(lines: list[str], path: Path) -> tuple[dict[str, str], int]:
    if not lines or lines[0].strip() != "---":
        raise OutboxError("missing_frontmatter_open", path)
    header: dict[str, str] = {}
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return header, idx + 1
        if not line.strip():
            raise OutboxError("malformed_frontmatter_blank_line", path)
        match = HEADER_RE.match(line)
        if not match:
            raise OutboxError("malformed_frontmatter_header", path)
        header[match.group(1).strip().lower()] = match.group(2).strip()
    raise OutboxError("missing_frontmatter_close", path)


def parse_outbox_message(path: Path) -> OutboxMessage:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header, body_start = _parse_header(lines, path)
    sentinels = list(SENTINEL_LINE_RE.finditer(text))
    if not sentinels:
        raise OutboxError("missing_done_sentinel", path)
    if len(sentinels) > 1:
        raise OutboxError("multiple_done_sentinels", path)
    sentinel = sentinels[0]
    before, after = text[: sentinel.start()], text[sentinel.end() :]
    if after.strip():
        raise OutboxError("trailing_content_after_sentinel", path)
    body_text = "\n".join(before.splitlines()[body_start:]).strip()
    from_agent = (header.get("from") or "").lower()
    to_agent = (header.get("to") or "").lower()
    status = (header.get("status") or "").lower()
    summary = header.get("summary") or ""
    if from_agent not in VALID_AGENTS:
        raise OutboxError("invalid_from_agent", path)
    if to_agent not in VALID_AGENTS:
        raise OutboxError("invalid_to_agent", path)
    if from_agent == to_agent:
        raise OutboxError("self_addressed_message", path)
    if status not in VALID_STATUSES:
        raise OutboxError("invalid_status", path)
    if not summary:
        raise OutboxError("missing_summary", path)
    if not body_text:
        raise OutboxError("missing_body", path)
    blocked_reason = header.get("blocked_reason") or header.get("blocked-reason")
    source_hash = hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
    return OutboxMessage(
        path=path,
        from_agent=from_agent,
        to_agent=to_agent,
        status=status,
        summary=summary,
        body=body_text,
        source_hash=source_hash,
        blocked_reason=blocked_reason,
    )


def iter_outbox_files(root: Path, room_id: str) -> Iterable[Path]:
    base = outbox_root(root, room_id)
    for agent in sorted(VALID_AGENTS):
        folder = base / agent
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.name.endswith(".pending.md") or path.name.endswith(".draft.md"):
                continue
            yield path


def import_outbox_messages(conn: sqlite3.Connection, *, root: Path, room_id: str) -> list[dict[str, object]]:
    imported: list[dict[str, object]] = []
    for path in iter_outbox_files(root, room_id):
        msg = parse_outbox_message(path)
        rel_path = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        existing_path_hash = message_source_hash_for_path(conn, room_id=room_id, source_type="outbox", source_path=rel_path)
        if existing_path_hash == msg.source_hash:
            continue
        if existing_path_hash is not None:
            raise OutboxError("outbox_file_changed_after_import", path)
        if has_message_source(conn, room_id=room_id, source_type="outbox", source_hash=msg.source_hash):
            continue
        rv = send_message(
            conn,
            root=root,
            room_id=room_id,
            from_agent=msg.from_agent,
            to_agent=msg.to_agent,
            kind="outbox",
            status=msg.status,
            summary=msg.summary,
            body=msg.body,
            blocked_reason=msg.blocked_reason,
            source_type="outbox",
            source_path=rel_path,
            source_hash=msg.source_hash,
        )
        imported.append({"message_id": rv["message_id"], "path": path, "message": msg})
    return imported
