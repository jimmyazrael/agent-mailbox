from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pane_control import build_list_argv, build_spawn_argv, build_split_argv

WELL_KNOWN_PATHS = [
    Path("C:/Program Files/WezTerm/wezterm.exe"),
    Path("C:/Program Files (x86)/WezTerm/wezterm.exe"),
    Path("/usr/local/bin/wezterm"),
    Path("/usr/bin/wezterm"),
]


def find_wezterm() -> Path:
    found = shutil.which("wezterm")
    if found:
        return Path(found)
    for path in WELL_KNOWN_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError("wezterm not found on PATH or in well-known install dirs")


def ensure_mux_alive(wezterm_exe: Path, *, max_wait_s: float = 5.0) -> None:
    list_argv = build_list_argv(wezterm_exe=wezterm_exe)
    rv = subprocess.run(list_argv, capture_output=True, text=True)
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
        rv = subprocess.run(list_argv, capture_output=True, text=True)
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


def _parse_pane_id(stdout: str) -> Optional[int]:
    text = (stdout or "").strip()
    return int(text) if text.isdigit() else None


def _list_pane_ids_in(wezterm_exe: Path, workspace: str) -> List[int]:
    rv = subprocess.run(build_list_argv(wezterm_exe=wezterm_exe), capture_output=True, text=True)
    rv.check_returncode()
    return [int(pane["pane_id"]) for pane in lookup_pane(parse_wezterm_list(rv.stdout), workspace=workspace)]


def launch_workspace(
    *,
    wezterm_exe: Path,
    workspace: str,
    cwd: Path,
    claude_cmd: List[str],
    codex_cmd: List[str],
) -> Dict[str, Any]:
    try:
        pre_ids = set(_list_pane_ids_in(wezterm_exe, workspace))
    except subprocess.CalledProcessError:
        pre_ids = set()
    rv = subprocess.run(
        build_spawn_argv(wezterm_exe=wezterm_exe, workspace=workspace, cwd=cwd, cmd=claude_cmd),
        capture_output=True,
        text=True,
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
