from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


def _debug_timing(message: str) -> None:
    if os.environ.get("AGENT_MAILBOX_DEBUG_TIMING") == "1":
        print(f"[agent-mailbox timing] {message}", file=sys.stderr, flush=True)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    else:
        proc.kill()


def _run_wezterm_list(list_argv: List[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        list_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(list_argv, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return subprocess.CompletedProcess(
            list_argv,
            124,
            stdout or "",
            (stderr or "") + f"wezterm list timed out after {timeout}s",
        )


def ensure_mux_alive(wezterm_exe: Path, *, max_wait_s: float = 5.0) -> None:
    list_argv = build_list_argv(wezterm_exe=wezterm_exe)
    _debug_timing("ensure_mux_alive:list initial start")
    rv = _run_wezterm_list(list_argv)
    _debug_timing(f"ensure_mux_alive:list initial done rc={rv.returncode}")
    if rv.returncode == 0:
        return
    # Starting wezterm-mux-server.exe directly on Windows can create a headless
    # mux that `wezterm cli spawn` talks to, while `wezterm cli list` observes a
    # different/default domain. Start through wezterm itself so the GUI and mux
    # agree on the domain.
    subprocess.Popen(
        [str(wezterm_exe), "start", "--always-new-process"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.time() + max_wait_s
    last_error = rv.stderr
    while time.time() < deadline:
        _debug_timing("ensure_mux_alive:list poll start")
        rv = _run_wezterm_list(list_argv)
        _debug_timing(f"ensure_mux_alive:list poll done rc={rv.returncode}")
        if rv.returncode == 0:
            return
        last_error = rv.stderr
        time.sleep(0.25)
    raise RuntimeError(f"wezterm mux did not become available within {max_wait_s}s: {last_error}")


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
        stdin=subprocess.DEVNULL,
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
        stdin=subprocess.DEVNULL,
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


def _raise_cli_error(action: str, rv: subprocess.CompletedProcess[str]) -> None:
    detail = " ".join(
        part
        for part in [
            f"rc={rv.returncode}",
            f"stdout={rv.stdout.strip()!r}" if rv.stdout else "",
            f"stderr={rv.stderr.strip()!r}" if rv.stderr else "",
        ]
        if part
    )
    raise RuntimeError(f"wezterm {action} failed: {detail}")


def _resolve_new_pane_id(*, label: str, stdout: str, before: set[int], after: set[int]) -> int:
    stdout_id = _parse_pane_id(stdout)
    if stdout_id is not None and stdout_id in after:
        return stdout_id
    new_ids = after - before
    if len(new_ids) != 1:
        raise RuntimeError(f"unable to identify new {label} pane: stdout={stdout_id!r} new_ids={new_ids}")
    return next(iter(new_ids))


def _wait_for_new_pane_id(
    *,
    label: str,
    stdout: str,
    before: set[int],
    list_ids,
    timeout_s: float = 5.0,
) -> int:
    deadline = time.time() + timeout_s
    last_after: set[int] = set()
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            last_after = set(list_ids())
            return _resolve_new_pane_id(label=label, stdout=stdout, before=before, after=last_after)
        except RuntimeError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(
        f"unable to identify new {label} pane after {timeout_s}s: "
        f"stdout={_parse_pane_id(stdout)!r} new_ids={last_after - before} last_error={last_error}"
    )


def _list_pane_ids_in(wezterm_exe: Path, workspace: str) -> List[int]:
    _debug_timing(f"list_pane_ids start workspace={workspace}")
    rv = subprocess.run(
        build_list_argv(wezterm_exe=wezterm_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    if rv.returncode != 0:
        _raise_cli_error("list", rv)
    _debug_timing(f"list_pane_ids done workspace={workspace}")
    return [int(pane["pane_id"]) for pane in lookup_pane(parse_wezterm_list(rv.stdout), workspace=workspace)]


def attach_workspace_gui(wezterm_exe: Path, workspace: str, cwd: Path) -> None:
    """Make the task workspace visible in a GUI window.

    `wezterm cli --prefer-mux spawn` can create panes in the mux without
    reliably surfacing a Windows GUI. Starting with `--attach` is the visible
    contract users expect from mailbox start/launch-tui.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        # On this Windows WezTerm build, `start --attach` requires an
        # explicit domain. `unix` is the local mux domain name even on
        # Windows for the default local multiplexer.
        build_start_argv(wezterm_exe=wezterm_exe, workspace=workspace, cwd=cwd, attach=True, domain="unix"),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
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
    _debug_timing("launch_workspace:spawn claude start")
    rv = subprocess.run(
        build_spawn_argv(wezterm_exe=wezterm_exe, workspace=workspace, cwd=cwd, cmd=claude_cmd),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    if rv.returncode != 0:
        _raise_cli_error("spawn claude", rv)
    _debug_timing("launch_workspace:spawn claude done")
    claude_id = _wait_for_new_pane_id(
        label="claude",
        stdout=rv.stdout,
        before=pre_ids,
        list_ids=lambda: _list_pane_ids_in(wezterm_exe, workspace),
    )
    pre_codex = set(_list_pane_ids_in(wezterm_exe, workspace))
    _debug_timing("launch_workspace:split codex start")
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
        stdin=subprocess.DEVNULL,
    )
    if rv2.returncode != 0:
        _raise_cli_error("split codex", rv2)
    _debug_timing("launch_workspace:split codex done")
    codex_id = _wait_for_new_pane_id(
        label="codex",
        stdout=rv2.stdout,
        before=pre_codex,
        list_ids=lambda: _list_pane_ids_in(wezterm_exe, workspace),
    )
    return {
        "workspace": workspace,
        "claude_pane_id": claude_id,
        "codex_pane_id": codex_id,
        "spawned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
