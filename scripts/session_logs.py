from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional


def _text_from_blocks(blocks: Any, *, text_types: set[str]) -> str:
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") in text_types:
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def codex_completed_text(record: dict[str, Any]) -> Optional[str]:
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload") or {}
    if payload.get("role") != "assistant" or payload.get("phase") != "final_answer":
        return None
    text = _text_from_blocks(payload.get("content"), text_types={"output_text"})
    return text or None


def claude_completed_text(record: dict[str, Any]) -> Optional[str]:
    if record.get("type") != "assistant":
        return None
    msg = record.get("message") or {}
    if msg.get("role") != "assistant" or msg.get("stop_reason") != "end_turn":
        return None
    text = _text_from_blocks(msg.get("content"), text_types={"text"})
    return text or None


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def latest_completed_turn(path: Path, *, agent: str) -> Optional[dict[str, Any]]:
    extractor = codex_completed_text if agent == "codex" else claude_completed_text
    latest: Optional[dict[str, Any]] = None
    for record in _iter_jsonl(path):
        text = extractor(record)
        if not text:
            continue
        latest = {
            "path": str(path),
            "timestamp": record.get("timestamp"),
            "text": text,
            "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "mtime": path.stat().st_mtime,
        }
    return latest


def find_codex_session_log(*, session_id: str, sessions_root: Optional[Path] = None) -> Optional[Path]:
    root = sessions_root or (Path.home() / ".codex" / "sessions")
    if not session_id or not root.exists():
        return None
    suffix = f"{session_id}.jsonl"
    matches = sorted(root.glob(f"**/rollout-*{suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _claude_project_dir(cwd: Path, projects_root: Path) -> Path:
    text = str(cwd).replace("\\", "-").replace("/", "-").replace(":", "-")
    return projects_root / text.strip("-")


def find_claude_session_log(
    *,
    session_id: str,
    project_cwd: Path,
    projects_root: Optional[Path] = None,
) -> Optional[Path]:
    root = projects_root or (Path.home() / ".claude" / "projects")
    if not session_id or not root.exists():
        return None
    candidates = [
        _claude_project_dir(project_cwd, root) / f"{session_id}.jsonl",
        root / str(project_cwd).replace("\\", "-").replace("/", "-") / f"{session_id}.jsonl",
    ]
    for path in candidates:
        if path.is_file():
            return path
    for path in sorted(root.glob(f"**/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        return path
    return None


def find_session_log(
    *,
    agent: str,
    session_id: str,
    project_cwd: Path,
    codex_sessions_root: Optional[Path] = None,
    claude_projects_root: Optional[Path] = None,
) -> Optional[Path]:
    if agent == "codex":
        return find_codex_session_log(session_id=session_id, sessions_root=codex_sessions_root)
    if agent == "claude":
        return find_claude_session_log(session_id=session_id, project_cwd=project_cwd, projects_root=claude_projects_root)
    raise ValueError(f"unknown agent: {agent}")
