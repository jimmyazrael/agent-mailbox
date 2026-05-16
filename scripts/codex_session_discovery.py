from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from mailbox_lib import path_eq, utc_now


def _iter_rollouts(sessions_root: Path, max_age_days: int) -> list[Path]:
    if not sessions_root.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    files = sorted(sessions_root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[Path] = []
    for path in files:
        file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        try:
            file_time = datetime(
                int(path.parent.parent.parent.name),
                int(path.parent.parent.name),
                int(path.parent.name),
                tzinfo=timezone.utc,
            )
        except (ValueError, IndexError):
            pass
        if file_time >= cutoff:
            out.append(path)
    return out


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                chunks.append(str(item.get("text") or item.get("input_text") or ""))
        return "\n".join(chunks)
    return ""


def _user_payload_has_marker(obj: dict[str, Any], marker: str) -> bool:
    typ = obj.get("type")
    payload = obj.get("payload") or {}
    if typ == "response_item":
        role = payload.get("role")
        if role not in (None, "", "user"):
            return False
        return marker in _content_text(payload.get("content"))
    if typ == "event_msg" and payload.get("type") == "user_message":
        return marker in (str(payload.get("message") or "") + "\n" + _content_text(payload.get("content")))
    return False


def _scan_file(path: Path, *, marker: str, project_cwd: Path, max_lines: int) -> Optional[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:max_lines]
    if not lines:
        return None
    try:
        meta = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    if meta.get("type") != "session_meta":
        return None
    payload = meta.get("payload") or {}
    session_id = payload.get("id")
    cwd = payload.get("cwd")
    if not session_id or not cwd or not path_eq(Path(cwd), project_cwd):
        return None
    marker_seen = False
    for line in lines[1:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if marker in line:
                marker_seen = True
                break
            continue
        if _user_payload_has_marker(obj, marker):
            marker_seen = True
            break
    return str(session_id) if marker_seen else None


def find_codex_session_id(
    *,
    task_id: str,
    project_cwd: Path,
    sessions_root: Optional[Path] = None,
    max_age_days: int = 7,
    max_lines: int = 200,
) -> Dict[str, Any]:
    marker = f"AGENT_MAILBOX_TASK_ID={task_id}"
    root = sessions_root or (Path.home() / ".codex" / "sessions")
    scanned = 0
    for path in _iter_rollouts(root, max_age_days):
        scanned += 1
        try:
            session_id = _scan_file(path, marker=marker, project_cwd=project_cwd, max_lines=max_lines)
        except OSError:
            continue
        if session_id:
            return {
                "status": "discovered",
                "session_id": session_id,
                "scanned_files": scanned,
                "attempted_at": utc_now(),
            }
    return {"status": "failed", "session_id": None, "scanned_files": scanned, "attempted_at": utc_now()}
