from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pane_control import build_get_text_argv, build_list_argv, build_spawn_argv, build_split_argv, build_start_argv

WEZTERM_WELL_KNOWN_PATHS = [
    Path("C:/Program Files/WezTerm/wezterm.exe"),
    Path("C:/Program Files (x86)/WezTerm/wezterm.exe"),
    Path("/usr/local/bin/wezterm"),
    Path("/usr/bin/wezterm"),
]


def find_wezterm() -> Path:
    found = shutil.which("wezterm")
    if found:
        return Path(found)
    for path in WEZTERM_WELL_KNOWN_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError("wezterm not found on PATH or in well-known install dirs")


def find_codex() -> Path | None:
    """Prefer the VS Code bundled Codex binary over stale npm shims on Windows."""
    extension_roots = sorted((Path.home() / ".vscode" / "extensions").glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
    if extension_roots:
        return extension_roots[-1]
    found = shutil.which("codex")
    return Path(found) if found else None


def ensure_mux_alive(wezterm_exe: Path, *, max_wait_s: float = 5.0) -> None:
    list_argv = build_list_argv(wezterm_exe=wezterm_exe)
    rv = subprocess.run(list_argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if rv.returncode == 0:
        return
    mux_exe = wezterm_exe.parent / ("wezterm-mux-server.exe" if os.name == "nt" else "wezterm-mux-server")
    if mux_exe.exists():
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        subprocess.Popen(
            [str(mux_exe), "start"],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [str(wezterm_exe), "start", "--always-new-process"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        rv = subprocess.run(list_argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if rv.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError(f"wezterm mux did not become available within {max_wait_s}s")


def parse_wezterm_list(stdout: str) -> List[Dict[str, Any]]:
    data = json.loads(stdout or "[]")
    if not isinstance(data, list):
        raise ValueError("expected JSON array from wezterm list")
    return [row for row in data if isinstance(row, dict) and "pane_id" in row]


def lookup_pane(
    panes: List[Dict[str, Any]],
    *,
    workspace: Optional[str] = None,
    pane_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    out = panes
    if workspace is not None:
        out = [pane for pane in out if pane.get("workspace") == workspace]
    if pane_id is not None:
        out = [pane for pane in out if int(pane.get("pane_id", -1)) == int(pane_id)]
    return out


def get_pane_text(wezterm_exe: Path, pane_id: int, *, start_line: int = -80) -> str:
    rv = subprocess.run(
        build_get_text_argv(wezterm_exe=wezterm_exe, pane_id=pane_id, start_line=start_line),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    rv.check_returncode()
    return rv.stdout or ""


def classify_agent_pane(text: str, *, agent: str) -> str:
    tail = "\n".join((text or "").splitlines()[-30:]).lower()
    full = (text or "").lower()
    if "auth_unavailable" in full or "no auth available" in full:
        return "auth_unavailable"
    if "unexpected status 503" in full or "service unavailable" in full:
        return "service_unavailable"
    if "403" in full and ("forbidden" in full or "unauthorized" in full):
        return "auth_403"
    if "is not recognized as an internal or external command" in full:
        return "shell_error"
    if "update now" in tail and "skip until next version" in tail:
        return "update_prompt"
    if "do you trust" in tail or "yes, i trust" in tail or "yes, continue" in tail:
        return "trust_prompt"
    if agent == "codex" and ("openai codex" in full or ">_ openai codex" in full):
        return "ready"
    if agent == "claude" and "claude code" in full:
        return "ready"
    if "clink" in tail or "copyright (c)" in tail or "windows\\system32\\cmd" in tail:
        return "shell"
    return "unknown"


def validate_workspace_startup(
    *,
    wezterm_exe: Path,
    workspace: str,
    claude_pane_id: int,
    codex_pane_id: int,
) -> Dict[str, Any]:
    rv = subprocess.run(
        build_list_argv(wezterm_exe=wezterm_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    rv.check_returncode()
    panes = parse_wezterm_list(rv.stdout)
    workspace_panes = lookup_pane(panes, workspace=workspace)
    by_id = {int(pane["pane_id"]): pane for pane in workspace_panes}
    status: Dict[str, Any] = {
        "workspace": workspace,
        "visible": bool(workspace_panes),
        "pane_count": len(workspace_panes),
        "agents": {},
        "ready": False,
    }
    for agent, pane_id in {"claude": claude_pane_id, "codex": codex_pane_id}.items():
        if int(pane_id) not in by_id:
            state = "missing_pane"
        else:
            try:
                state = classify_agent_pane(get_pane_text(wezterm_exe, int(pane_id)), agent=agent)
            except subprocess.CalledProcessError:
                state = "unreadable_pane"
        status["agents"][agent] = {"pane_id": int(pane_id), "state": state}
    status["ready"] = bool(status["visible"]) and all(agent["state"] == "ready" for agent in status["agents"].values())
    return status


def _parse_pane_id(stdout: str) -> Optional[int]:
    text = (stdout or "").strip()
    return int(text) if text.isdigit() else None


def _list_pane_ids_in(wezterm_exe: Path, workspace: str) -> List[int]:
    rv = subprocess.run(
        build_list_argv(wezterm_exe=wezterm_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    rv.check_returncode()
    return [int(pane["pane_id"]) for pane in lookup_pane(parse_wezterm_list(rv.stdout), workspace=workspace)]


def attach_workspace_gui(wezterm_exe: Path, workspace: str, cwd: Path) -> None:
    """Make the task workspace visible in a GUI window.

    `wezterm cli --prefer-mux spawn` can create panes in the mux without
    reliably surfacing a Windows GUI. Starting with `--attach` is the visible
    contract users expect from mailbox start/launch-tui.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        # On this Windows WezTerm build, `start --attach` requires an
        # explicit domain. `unix` is the local mux domain name even on
        # Windows for the default local multiplexer.
        build_start_argv(wezterm_exe=wezterm_exe, workspace=workspace, cwd=cwd, attach=True, domain="unix"),
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def launch_workspace(
    *,
    wezterm_exe: Path,
    workspace: str,
    cwd: Path,
    claude_cmd: List[str],
    codex_cmd: List[str],
    env: Optional[dict[str, str]] = None,
) -> Dict[str, Any]:
    try:
        pre_ids = set(_list_pane_ids_in(wezterm_exe, workspace))
    except subprocess.CalledProcessError:
        pre_ids = set()
    rv = subprocess.run(
        build_spawn_argv(wezterm_exe=wezterm_exe, workspace=workspace, cwd=cwd, cmd=claude_cmd),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    rv.check_returncode()
    claude_id = _parse_pane_id(rv.stdout)
    if claude_id is None:
        new_ids = set(_list_pane_ids_in(wezterm_exe, workspace)) - pre_ids
        if len(new_ids) != 1:
            raise RuntimeError(f"unable to identify new claude pane: {new_ids}")
        claude_id = next(iter(new_ids))
    pre_codex = set(_list_pane_ids_in(wezterm_exe, workspace))
    rv2 = subprocess.run(
        build_split_argv(
            wezterm_exe=wezterm_exe,
            source_pane_id=claude_id,
            direction="right",
            percent=50,
            cwd=cwd,
            cmd=codex_cmd,
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    rv2.check_returncode()
    codex_id = _parse_pane_id(rv2.stdout)
    if codex_id is None:
        new_ids = set(_list_pane_ids_in(wezterm_exe, workspace)) - pre_codex
        if len(new_ids) != 1:
            raise RuntimeError(f"unable to identify new codex pane: {new_ids}")
        codex_id = next(iter(new_ids))
    return {
        "workspace": workspace,
        "claude_pane_id": claude_id,
        "codex_pane_id": codex_id,
        "spawned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
