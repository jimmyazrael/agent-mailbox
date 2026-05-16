from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROTOCOL_VERSION = 1
DEFAULT_PARTICIPANTS = ("codex", "claude")
VALID_STATE_STATUSES = {"waiting", "running", "blocked", "final", "error"}
VALID_MESSAGE_STATUSES = {"continue", "blocked", "final", "error"}
STATUS_RE = re.compile(r"^MAILBOX_STATUS:\s*(continue|final|blocked(?:\s*-\s*.*)?|error(?:\s*-\s*.*)?)\s*$", re.IGNORECASE | re.MULTILINE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_root() -> Path:
    return Path.home() / ".agent-mailbox"


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def task_dir(root: Path, task_id: str) -> Path:
    return root / task_id


def state_path(root: Path, task_id: str) -> Path:
    return task_dir(root, task_id) / "state.json"


def messages_path(root: Path, task_id: str) -> Path:
    return task_dir(root, task_id) / "messages.md"


def relay_log_path(root: Path, task_id: str) -> Path:
    return task_dir(root, task_id) / "relay.log"


def lock_path(root: Path, task_id: str) -> Path:
    return task_dir(root, task_id) / "state.lock"


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=default_root(), help="Mailbox runtime root")
    parser.add_argument("--task-id", required=True, help="Mailbox task id")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


@contextmanager
def file_lock(root: Path, task_id: str, owner: str = "mailbox", timeout: float = 10.0, stale_after: float = 60.0):
    path = lock_path(root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    payload = {"owner": owner, "pid": os.getpid(), "created_at": utc_now()}
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                if age > stale_after:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for mailbox lock: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def load_state(root: Path, task_id: str) -> Dict[str, Any]:
    return read_json(state_path(root, task_id))


def save_state(root: Path, task_id: str, state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json_atomic(state_path(root, task_id), state)


def create_initial_state(
    task_id: str,
    goal: str,
    project_cwd: Path,
    first_turn: str,
    participants: Iterable[str] = DEFAULT_PARTICIPANTS,
    max_rounds: int = 30,
    turn_timeout_seconds: int = 300,
    max_peer_message_chars: int = 10000,
    max_total_cost_usd: Optional[float] = 5.0,
    max_total_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    participant_list = list(participants)
    if first_turn not in participant_list:
        raise ValueError(f"first_turn must be one of participants: {participant_list}")
    now = utc_now()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "task_id": task_id,
        "goal": goal,
        "project_cwd": str(project_cwd),
        "participants": participant_list,
        "turn": first_turn,
        "status": "waiting",
        "round": 0,
        "last_message_id": 0,
        "created_at": now,
        "updated_at": now,
        "sessions": {
            "codex": {"thread_id": None},
            "claude": {"session_id": None},
        },
        "limits": {
            "max_rounds": max_rounds,
            "turn_timeout_seconds": turn_timeout_seconds,
            "max_peer_message_chars": max_peer_message_chars,
            "max_total_cost_usd": max_total_cost_usd,
            "max_total_tokens": max_total_tokens,
        },
        "usage": {
            "known_cost_usd": 0.0,
            "codex_input_tokens": 0,
            "codex_output_tokens": 0,
        },
        "processing": None,
    }


def init_mailbox(
    root: Path,
    task_id: str,
    goal: str,
    project_cwd: Path,
    first_turn: str = "codex",
    participants: Iterable[str] = DEFAULT_PARTICIPANTS,
    max_rounds: int = 30,
    turn_timeout_seconds: int = 300,
    max_peer_message_chars: int = 10000,
    max_total_cost_usd: Optional[float] = 5.0,
    max_total_tokens: Optional[int] = None,
    force: bool = False,
) -> Dict[str, Any]:
    root = root.expanduser()
    tdir = task_dir(root, task_id)
    state_file = state_path(root, task_id)
    if state_file.exists() and not force:
        raise FileExistsError(f"mailbox task already exists: {state_file}")
    tdir.mkdir(parents=True, exist_ok=True)
    (root / "index.md").touch(exist_ok=True)
    state = create_initial_state(
        task_id=task_id,
        goal=goal,
        project_cwd=project_cwd.resolve(),
        first_turn=first_turn,
        participants=participants,
        max_rounds=max_rounds,
        turn_timeout_seconds=turn_timeout_seconds,
        max_peer_message_chars=max_peer_message_chars,
        max_total_cost_usd=max_total_cost_usd,
        max_total_tokens=max_total_tokens,
    )
    write_json_atomic(state_file, state)
    messages_path(root, task_id).write_text(f"# Agent Mailbox Task: {task_id}\n\nGoal: {goal}\n", encoding="utf-8", newline="\n")
    with (root / "index.md").open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"- {utc_now()} `{task_id}`: {goal}\n")
    return state


def normalize_message_status(marker: str) -> tuple[str, Optional[str]]:
    text = marker.strip()
    low = text.lower()
    if low.startswith("blocked"):
        reason = text.split("-", 1)[1].strip() if "-" in text else None
        return "blocked", reason
    if low.startswith("error"):
        reason = text.split("-", 1)[1].strip() if "-" in text else None
        return "error", reason
    if low == "final":
        return "final", None
    return "continue", None


def parse_status_marker(output: str) -> tuple[str, Optional[str]]:
    matches = list(STATUS_RE.finditer(output or ""))
    if not matches:
        return "continue", None
    return normalize_message_status(matches[-1].group(1))


def peer_for(state: Dict[str, Any], author: str) -> str:
    participants = state.get("participants", list(DEFAULT_PARTICIPANTS))
    for participant in participants:
        if participant != author:
            return participant
    raise ValueError(f"cannot find peer for {author}")


def append_message_locked(
    root: Path,
    task_id: str,
    state: Dict[str, Any],
    author: str,
    target: str,
    status: str,
    summary: str,
    content: str,
    next_turn: Optional[str],
    state_updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if status not in VALID_MESSAGE_STATUSES:
        raise ValueError(f"invalid message status: {status}")
    message_id = int(state.get("last_message_id", 0)) + 1
    timestamp = utc_now()
    block = (
        "\n---\n\n"
        f"## MSG {message_id} - {author} -> {target}\n\n"
        f"message_id: {message_id}\n"
        f"author: {author}\n"
        f"target: {target}\n"
        f"status: {status}\n"
        f"timestamp: {timestamp}\n"
        f"summary: {summary.strip()}\n\n"
        "### Content\n\n"
        f"{content.rstrip()}\n"
    )
    with messages_path(root, task_id).open("a", encoding="utf-8", newline="\n") as f:
        f.write(block)
    state["last_message_id"] = message_id
    state["processing"] = None
    if status == "continue":
        state["status"] = "waiting"
        state["turn"] = next_turn
        state["round"] = int(state.get("round", 0)) + 1
    elif status in {"blocked", "final", "error"}:
        state["status"] = status
        state["turn"] = None
    if state_updates:
        deep_update(state, state_updates)
    save_state(root, task_id, state)
    return state


def post_message(
    root: Path,
    task_id: str,
    author: str,
    target: str,
    status: str,
    summary: str,
    content: str,
    next_turn: Optional[str] = None,
    state_updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with file_lock(root, task_id, owner=f"post:{author}"):
        state = load_state(root, task_id)
        if next_turn is None and status == "continue":
            next_turn = target
        return append_message_locked(root, task_id, state, author, target, status, summary, content, next_turn, state_updates)


def deep_update(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value


def parse_messages(text: str) -> List[Dict[str, Any]]:
    chunks = re.split(r"\n---\n\n(?=## MSG \d+ - )", text)
    messages: List[Dict[str, Any]] = []
    for chunk in chunks:
        if not chunk.startswith("## MSG "):
            continue
        header, _, content = chunk.partition("### Content")
        meta: Dict[str, Any] = {}
        title = header.splitlines()[0]
        id_match = re.match(r"## MSG (\d+) - (.*?) -> (.*)", title)
        if id_match:
            meta["message_id"] = int(id_match.group(1))
            meta["author"] = id_match.group(2).strip()
            meta["target"] = id_match.group(3).strip()
        for line in header.splitlines()[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        meta["content"] = content.lstrip("\n").rstrip()
        messages.append(meta)
    return messages


def read_messages(root: Path, task_id: str) -> List[Dict[str, Any]]:
    path = messages_path(root, task_id)
    if not path.exists():
        return []
    return parse_messages(path.read_text(encoding="utf-8"))


def latest_message(root: Path, task_id: str) -> Optional[Dict[str, Any]]:
    messages = read_messages(root, task_id)
    return messages[-1] if messages else None


def cdata_safe(text: str) -> str:
    return (text or "").replace("]]>", "]]&gt;")


def bounded(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n\n[mailbox relay truncated {omitted} chars]"


def copytree_replace(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)
