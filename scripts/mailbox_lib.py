from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

PROTOCOL_VERSION = 1
IS_WINDOWS = os.name == "nt"

VALID_MESSAGE_STATUSES = {"continue", "blocked", "final", "error"}
VALID_ROOM_STATUSES = {"waiting", "running", "blocked", "final", "error", "stopped", "paused"}
TERMINAL_STATUSES = {"final", "error", "stopped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_root() -> Path:
    env = os.environ.get("AGENT_MAILBOX_ROOT")
    return Path(env) if env else Path.home() / ".agent-mailbox"


def path_eq(a: Path, b: Path) -> bool:
    try:
        ar = a.resolve()
        br = b.resolve()
    except Exception:
        ar, br = a, b
    if IS_WINDOWS:
        return str(ar).replace("\\", "/").lower() == str(br).replace("\\", "/").lower()
    return str(ar) == str(br)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return slug or "task"


def generate_task_id(prefix: str, label: str = "") -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    parts = [_slug(prefix)]
    if label:
        parts.append(_slug(label))
    parts.append(ts)
    return "-".join(parts)


def peer_for(participants: Iterable[str], author: str) -> str:
    peers = [p for p in participants if p != author]
    if len(peers) != 1:
        raise ValueError(f"expected exactly one peer for {author}, found {peers}")
    return peers[0]


def bounded(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[truncated {len(text) - max_chars} chars]"


def cdata_safe(text: str) -> str:
    return (text or "").replace("]]>", "]]]]><![CDATA[>")


def pid_exists(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


STATUS_RE = re.compile(
    r"^MAILBOX_STATUS:\s*(continue|final|blocked(?:\s*-\s*.*)?|error(?:\s*-\s*.*)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_status_marker(text: str) -> tuple[str, Optional[str]]:
    matches = list(STATUS_RE.finditer(text or ""))
    if not matches:
        return "continue", None
    raw = matches[-1].group(1).strip()
    low = raw.lower()
    if low.startswith("blocked"):
        return "blocked", raw.split("-", 1)[1].strip() if "-" in raw else None
    if low.startswith("error"):
        return "error", raw.split("-", 1)[1].strip() if "-" in raw else None
    if low == "final":
        return "final", None
    return "continue", None
