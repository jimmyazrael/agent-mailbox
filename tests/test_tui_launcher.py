import json
import subprocess
from pathlib import Path

import pytest

import tui_launcher
from pane_control import build_spawn_argv
from tui_launcher import (
    attach_workspace_gui,
    classify_agent_pane,
    ensure_mux_alive,
    find_codex,
    find_wezterm,
    launch_workspace,
    lookup_pane,
    parse_wezterm_list,
    validate_workspace_startup,
)


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

    def fake_list(argv, **kwargs):
        calls.append(argv)
        if "list" in argv and len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "no mux")
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    def fake_popen(argv, **kwargs):
        calls.append(argv)

        class P:
            pid = 999

        return P()

    monkeypatch.setattr(tui_launcher, "_run_wezterm_list", fake_list)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    ensure_mux_alive(wez, max_wait_s=1)
    assert any("start" in call for call in calls)


def test_ensure_mux_alive_fails_fast_when_wezterm_list_hangs(monkeypatch, tmp_path):
    def fake_list(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 124, "", "wezterm list timed out")

    def fake_popen(argv, **kwargs):
        class P:
            pid = 999

        return P()

    monkeypatch.setattr(tui_launcher, "_run_wezterm_list", fake_list)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    with pytest.raises(RuntimeError, match="timed out"):
        ensure_mux_alive(wez, max_wait_s=0.1)


def test_parse_and_lookup_wezterm_list():
    payload = json.dumps(
        [
            {"pane_id": 3, "workspace": "agent-mailbox-t1", "window_id": 1, "tab_id": 1},
            {"pane_id": 4, "workspace": "default", "window_id": 2, "tab_id": 2},
        ]
    )
    panes = parse_wezterm_list(payload)
    assert {p["pane_id"] for p in lookup_pane(panes, workspace="agent-mailbox-t1")} == {3}


def test_classify_agent_pane_states():
    assert classify_agent_pane(">_ OpenAI Codex\nmodel: gpt-5.4", agent="codex") == "ready"
    assert classify_agent_pane("Claude Code v2.1.143\nWelcome back", agent="claude") == "ready"
    assert classify_agent_pane("Update now\nSkip until next version", agent="codex") == "update_prompt"
    assert classify_agent_pane("Do you trust the files in this folder?", agent="codex") == "trust_prompt"
    assert classify_agent_pane("unexpected status 503 Service Unavailable: auth_unavailable", agent="codex") == "auth_unavailable"
    assert classify_agent_pane("'foo' is not recognized as an internal or external command", agent="codex") == "shell_error"


def test_validate_workspace_startup_requires_visible_ready_agent_panes(monkeypatch, tmp_path):
    panes = json.dumps(
        [
            {"pane_id": 11, "workspace": "agent-mailbox-t1", "window_id": 1},
            {"pane_id": 12, "workspace": "agent-mailbox-t1", "window_id": 1},
        ]
    )

    def fake_run(argv, **kwargs):
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, panes, "")
        if "get-text" in argv and "11" in argv:
            return subprocess.CompletedProcess(argv, 0, "Claude Code v2.1.143", "")
        if "get-text" in argv and "12" in argv:
            return subprocess.CompletedProcess(argv, 0, ">_ OpenAI Codex", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")

    monkeypatch.setattr("subprocess.run", fake_run)
    status = validate_workspace_startup(
        wezterm_exe=tmp_path / "wezterm.exe",
        workspace="agent-mailbox-t1",
        claude_pane_id=11,
        codex_pane_id=12,
    )
    assert status["ready"] is True
    assert status["visible"] is True
    assert status["agents"]["claude"]["state"] == "ready"
    assert status["agents"]["codex"]["state"] == "ready"


def test_validate_workspace_startup_reports_prompt_not_ready(monkeypatch, tmp_path):
    panes = json.dumps(
        [
            {"pane_id": 11, "workspace": "agent-mailbox-t1", "window_id": 1},
            {"pane_id": 12, "workspace": "agent-mailbox-t1", "window_id": 1},
        ]
    )

    def fake_run(argv, **kwargs):
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, panes, "")
        if "get-text" in argv and "11" in argv:
            return subprocess.CompletedProcess(argv, 0, "Claude Code v2.1.143", "")
        if "get-text" in argv and "12" in argv:
            return subprocess.CompletedProcess(argv, 0, "Update now\nSkip until next version", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")

    monkeypatch.setattr("subprocess.run", fake_run)
    status = validate_workspace_startup(
        wezterm_exe=tmp_path / "wezterm.exe",
        workspace="agent-mailbox-t1",
        claude_pane_id=11,
        codex_pane_id=12,
    )
    assert status["ready"] is False
    assert status["agents"]["codex"]["state"] == "update_prompt"


def test_launch_workspace_captures_pane_ids_and_passes_env(monkeypatch, tmp_path):
    seen_env = []

    def fake_run(argv, **kwargs):
        seen_env.append(kwargs.get("env"))
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
        env={"AGENT_MAILBOX_CODEX_EXE": "C:/codex.exe"},
    )
    assert result["claude_pane_id"] == 11
    assert result["codex_pane_id"] == 12
    assert any(env and env["AGENT_MAILBOX_CODEX_EXE"] == "C:/codex.exe" for env in seen_env)


def test_find_codex_prefers_latest_vscode_extension(monkeypatch, tmp_path):
    old = tmp_path / ".vscode" / "extensions" / "openai.chatgpt-1" / "bin" / "windows-x86_64" / "codex.exe"
    new = tmp_path / ".vscode" / "extensions" / "openai.chatgpt-2" / "bin" / "windows-x86_64" / "codex.exe"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_bytes(b"")
    new.write_bytes(b"")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "C:/stale/codex.cmd")
    assert find_codex() == new


def test_attach_workspace_gui_starts_visible_client(monkeypatch, tmp_path):
    calls = []

    class P:
        pid = 123

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return P()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    attach_workspace_gui(wez, "agent-mailbox-t1", tmp_path)
    argv, kwargs = calls[0]
    assert argv[:2] == [str(wez), "start"]
    assert "--domain" in argv and "unix" in argv
    assert "--workspace" in argv and "agent-mailbox-t1" in argv
    assert "--attach" in argv
    assert "--cwd" in argv and str(tmp_path) in argv
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


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
