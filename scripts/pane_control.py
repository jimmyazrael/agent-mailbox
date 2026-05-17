from __future__ import annotations

from pathlib import Path
from typing import List, Optional


def _base(wezterm_exe: Path, prefer_mux: bool) -> List[str]:
    argv = [str(wezterm_exe), "cli"]
    if prefer_mux:
        argv.append("--prefer-mux")
    return argv


def build_spawn_argv(
    *,
    wezterm_exe: Path,
    workspace: str,
    cwd: Path,
    cmd: List[str],
    prefer_mux: bool = True,
    new_window: bool = True,
    window_id: Optional[int] = None,
) -> List[str]:
    argv = _base(wezterm_exe, prefer_mux) + ["spawn"]
    if new_window:
        argv += ["--new-window", "--workspace", workspace]
    elif window_id is not None:
        argv += ["--window-id", str(window_id)]
    argv += ["--cwd", str(cwd), "--"] + cmd
    return argv


def build_split_argv(
    *,
    wezterm_exe: Path,
    source_pane_id: int,
    direction: str,
    percent: Optional[int],
    cwd: Path,
    cmd: List[str],
    prefer_mux: bool = True,
) -> List[str]:
    flags = {"right": "--right", "bottom": "--bottom", "left": "--left", "top": "--top"}
    if direction not in flags:
        raise ValueError(f"unknown split direction: {direction}")
    argv = _base(wezterm_exe, prefer_mux) + ["split-pane", "--pane-id", str(source_pane_id), flags[direction]]
    if percent is not None:
        argv += ["--percent", str(percent)]
    argv += ["--cwd", str(cwd), "--"] + cmd
    return argv


def build_send_text_argv(
    *,
    wezterm_exe: Path,
    pane_id: int,
    text: str,
    no_paste: bool = True,
    prefer_mux: bool = True,
) -> List[str]:
    argv = _base(wezterm_exe, prefer_mux) + ["send-text", "--pane-id", str(pane_id)]
    if no_paste:
        argv.append("--no-paste")
    argv.append(text)
    return argv


def build_list_argv(*, wezterm_exe: Path, prefer_mux: bool = True) -> List[str]:
    return _base(wezterm_exe, prefer_mux) + ["list", "--format", "json"]


def build_get_text_argv(
    *,
    wezterm_exe: Path,
    pane_id: int,
    start_line: int = -50,
    prefer_mux: bool = True,
) -> List[str]:
    return _base(wezterm_exe, prefer_mux) + [
        "get-text",
        "--pane-id",
        str(pane_id),
        "--start-line",
        str(start_line),
    ]


def build_activate_pane_argv(*, wezterm_exe: Path, pane_id: int, prefer_mux: bool = True) -> List[str]:
    return _base(wezterm_exe, prefer_mux) + ["activate-pane", "--pane-id", str(pane_id)]
