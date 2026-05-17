import json
import subprocess
from pathlib import Path

import pytest

from pane_control import build_spawn_argv
from tui_launcher import ensure_mux_alive, find_wezterm, launch_workspace, lookup_pane, parse_wezterm_list


def test_find_wezterm_uses_path(monkeypatch, tmp_path):
    fake = tmp_path / "wezterm.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda name: str(fake) if name == "wezterm" else None)
    assert find_wezterm() == fake


def test_find_wezterm_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    with pytest.raises(FileNotFoundError):
        find_wezterm()


def test_ensure_mux_alive_starts_when_needed(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "list" in argv and len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "no mux")
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    def fake_popen(argv, **kwargs):
        calls.append(argv)

        class P:
            pid = 999

        return P()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    ensure_mux_alive(wez, max_wait_s=1)
    assert any("start" in call for call in calls)


def test_parse_and_lookup_wezterm_list():
    payload = json.dumps(
        [
            {"pane_id": 3, "workspace": "agent-mailbox-t1", "window_id": 1, "tab_id": 1},
            {"pane_id": 4, "workspace": "default", "window_id": 2, "tab_id": 2},
        ]
    )
    panes = parse_wezterm_list(payload)
    assert {p["pane_id"] for p in lookup_pane(panes, workspace="agent-mailbox-t1")} == {3}


def test_launch_workspace_captures_pane_ids(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        if "spawn" in argv:
            return subprocess.CompletedProcess(argv, 0, "11\n", "")
        if "split-pane" in argv:
            return subprocess.CompletedProcess(argv, 0, "12\n", "")
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    result = launch_workspace(
        wezterm_exe=wez,
        workspace="agent-mailbox-t1",
        cwd=tmp_path,
        claude_cmd=["cmd", "/c", "claude.cmd"],
        codex_cmd=["cmd", "/c", "codex.cmd"],
    )
    assert result["claude_pane_id"] == 11
    assert result["codex_pane_id"] == 12


def test_spawn_existing_window_uses_window_id_not_workspace(tmp_path):
    wez = tmp_path / "wezterm.exe"
    argv = build_spawn_argv(
        wezterm_exe=wez,
        workspace="agent-mailbox-t1",
        cwd=tmp_path,
        cmd=["cmd", "/c", "chat.cmd"],
        new_window=False,
        window_id=7,
    )
    assert "--window-id" in argv
    assert "7" in argv
    assert "--workspace" not in argv
    assert "--new-window" not in argv
