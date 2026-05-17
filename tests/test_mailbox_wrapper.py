import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_chat import connect_db, init_db, send_message

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX = SKILL_ROOT / "scripts" / "mailbox.py"


def _run(*args, env=None):
    return subprocess.run([sys.executable, str(MAILBOX), *args], capture_output=True, text=True, env=env)


def _seed_message(root: Path, task_id: str, *, from_agent: str, to_agent: str, status: str = "continue", summary: str = "hello", body: str = "body"):
    conn = connect_db(root)
    try:
        return send_message(
            conn,
            root=root,
            room_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            kind="message",
            status=status,
            summary=summary,
            body=body,
            next_turn=to_agent if status == "continue" else None,
        )
    finally:
        conn.close()


def test_init_creates_room_with_sessions(tmp_path):
    root = tmp_path / "mb"
    rv = _run(
        "init",
        "--root",
        str(root),
        "--prefix",
        "spc",
        "--label",
        "Phase Test",
        "--goal",
        "Test wrapper",
        "--project-cwd",
        str(tmp_path),
        "--format",
        "json",
    )
    assert rv.returncode == 0, rv.stderr
    out = json.loads(rv.stdout)
    task_id = out["data"]["task_id"]
    conn = connect_db(root)
    assert conn.execute("SELECT id FROM rooms WHERE id=?", (task_id,)).fetchone() is not None
    sessions = {row["agent"]: dict(row) for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,))}
    assert sessions["claude"]["session_id"]
    assert sessions["codex"]["discovery_status"] == "pending"


def test_init_context_file_creates_bootstrap_context_message(tmp_path):
    root = tmp_path / "mb"
    context = tmp_path / "context.md"
    context.write_text("Scope: inspect README first.\nDone: post final only after peer review.\n", encoding="utf-8")
    rv = _run(
        "init",
        "--root",
        str(root),
        "--prefix",
        "ctx",
        "--goal",
        "Use context",
        "--project-cwd",
        str(tmp_path),
        "--context-file",
        str(context),
        "--format",
        "json",
    )
    assert rv.returncode == 0, rv.stderr
    task_id = json.loads(rv.stdout)["data"]["task_id"]
    conn = connect_db(root)
    room = conn.execute("SELECT turn, round, last_message_id FROM rooms WHERE id=?", (task_id,)).fetchone()
    msg = conn.execute("SELECT * FROM messages WHERE room_id=?", (task_id,)).fetchone()
    assert dict(room) == {"turn": "claude", "round": 0, "last_message_id": 1}
    assert msg["from_agent"] == "user"
    assert msg["to_agent"] == "broadcast"
    assert msg["kind"] == "system"
    assert msg["summary"] == "bootstrap context"
    assert "inspect README first" in msg["body_text"]


def test_show_ack_export_and_status(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    _seed_message(root, task_id, from_agent="claude", to_agent="codex", status="continue", summary="hello", body="body")
    show = _run("show", "--root", str(root), "--task-id", task_id, "--body", "--format", "json")
    assert json.loads(show.stdout)["data"]["messages"][0]["body"] == "body"
    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["turn"] == "codex"
    export = _run("export", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert Path(json.loads(export.stdout)["data"]["path"]).exists()


def test_protocol_lint_warns_when_non_terminal_header_missing():
    import mailbox as mailbox_cli

    warnings = mailbox_cli.lint_protocol_header(status="continue", body="I agree we should run it.", participants={"claude", "codex"})
    assert "missing protocol header: Mode" in warnings
    assert "non-terminal posts must name a concrete Next action or Blocked on" in warnings


def test_protocol_lint_accepts_role_mode_protocol_header():
    import mailbox as mailbox_cli

    body = "\n".join(
        [
            "Mode: EXECUTE",
            "Coordinator: claude",
            "Owner: claude",
            "Reviewer: codex",
            "Next action: run AM-01",
            "Done when: result JSON is posted",
            "",
            "I will run it now.",
        ]
    )
    assert mailbox_cli.lint_protocol_header(status="continue", body=body, participants={"claude", "codex"}) == []


def test_protocol_lint_warns_owner_not_participant():
    import mailbox as mailbox_cli

    body = "\n".join(
        [
            "Mode: EXECUTE",
            "Coordinator: claude",
            "Owner: buildbot",
            "Reviewer: codex",
            "Next action: buildbot runs the next step",
            "Done when: tests pass",
        ]
    )
    assert "protocol Owner is not a participant: buildbot" in mailbox_cli.lint_protocol_header(status="continue", body=body, participants={"claude", "codex"})


def test_protocol_lint_accepts_lowercase_protocol_mode():
    import mailbox as mailbox_cli

    body = "\n".join(
        [
            "Mode: discuss",
            "Coordinator: claude",
            "Owner: claude",
            "Reviewer: codex",
            "Next action: compare options",
            "Done when: a decision is posted",
        ]
    )
    assert mailbox_cli.lint_protocol_header(status="continue", body=body, participants={"claude", "codex"}) == []


def test_send_message_accepts_coordinate_protocol_mode(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    body = "\n".join(
        [
            "Mode: Coordinate",
            "Coordinator: claude",
            "Owner: codex",
            "Reviewer: claude",
            "Next action: codex runs the scenario",
            "Done when: results are posted",
        ]
    )
    import mailbox as mailbox_cli

    assert mailbox_cli.lint_protocol_header(status="continue", body=body, participants={"claude", "codex"}) == []
    _seed_message(root, task_id, from_agent="claude", to_agent="codex", status="continue", summary="coordinate", body=body)
    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["turn"] == "codex"


def test_protocol_lint_rejects_arbitrary_protocol_mode():
    import mailbox as mailbox_cli

    body = "\n".join(
        [
            "Mode: lol",
            "Coordinator: claude",
            "Owner: claude",
            "Reviewer: codex",
            "Next action: do work",
            "Done when: done",
        ]
    )
    assert "unknown protocol Mode: lol" in mailbox_cli.lint_protocol_header(status="continue", body=body, participants={"claude", "codex"})


def test_protocol_lint_rejects_empty_protocol_mode():
    import mailbox as mailbox_cli

    body = "\n".join(
        [
            "Mode: ",
            "Coordinator: claude",
            "Owner: claude",
            "Reviewer: codex",
            "Next action: do work",
            "Done when: done",
        ]
    )
    assert "missing protocol header: Mode" in mailbox_cli.lint_protocol_header(status="continue", body=body, participants={"claude", "codex"})


def test_parser_has_no_public_post_command():
    import mailbox as mailbox_cli

    choices = mailbox_cli.build_parser()._subparsers._group_actions[0].choices
    assert "post" not in choices


def test_launch_tui_fake_panes(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    env = dict(**__import__("os").environ, AGENT_MAILBOX_FAKE_PANE_IDS="11,12,13,14,15", AGENT_MAILBOX_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"))
    rv = _run("launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json", env=env)
    assert rv.returncode == 0, rv.stderr
    data = json.loads(rv.stdout)["data"]
    assert data["claude_pane_id"] == 11
    assert data["chat_pane_id"] == 14
    assert data["control_pane_id"] == 15
    conn = connect_db(root)
    assert conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='relay'", (task_id,)).fetchone() is None
    chat = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='chat'", (task_id,)).fetchone()
    assert chat["pane_id"] == 14
    control = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='control'", (task_id,)).fetchone()
    assert control["pane_id"] == 15
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id=?", (task_id,)).fetchone()
    assert relay["paused"] == 0
    assert relay["pause_reason"] is None


def test_launch_tui_no_chat_disables_chat_fake_pane(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    env = dict(
        **__import__("os").environ,
        AGENT_MAILBOX_FAKE_PANE_IDS="11,12,13,14,15",
        AGENT_MAILBOX_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"),
    )
    rv = _run("launch-tui", "--root", str(root), "--task-id", task_id, "--no-chat", "--format", "json", env=env)
    assert rv.returncode == 0, rv.stderr
    data = json.loads(rv.stdout)["data"]
    assert data["chat_pane_id"] is None
    conn = connect_db(root)
    chat = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='chat'", (task_id,)).fetchone()
    assert chat is None
    control = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='control'", (task_id,)).fetchone()
    assert control["pane_id"] == 15


def test_launch_tui_no_control_panel_disables_control_fake_pane(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    env = dict(
        **__import__("os").environ,
        AGENT_MAILBOX_FAKE_PANE_IDS="11,12,13,14,15",
        AGENT_MAILBOX_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"),
    )
    rv = _run("launch-tui", "--root", str(root), "--task-id", task_id, "--no-control-panel", "--format", "json", env=env)
    assert rv.returncode == 0, rv.stderr
    data = json.loads(rv.stdout)["data"]
    assert data["control_pane_id"] is None
    conn = connect_db(root)
    control = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='control'", (task_id,)).fetchone()
    assert control is None


def test_launch_tui_does_not_bootstrap_codex_with_task_prompt(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    calls = []

    def fake_launch_workspace(**kwargs):
        calls.append(kwargs)
        return {"workspace": "w", "claude_pane_id": 11, "codex_pane_id": 12, "spawned_at": "now"}

    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr("tui_launcher.find_codex", lambda: tmp_path / "codex.exe")
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": True,
            "visible": True,
            "agents": {"claude": {"state": "ready"}, "codex": {"state": "ready"}},
        },
    )
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda *args, **kwargs: None)
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["launch-tui", "--root", str(root), "--task-id", task_id, "--no-chat", "--no-control-panel", "--format", "json"])
    assert mailbox_cli.cmd_launch_tui(args) == 0
    codex_cmd = calls[0]["codex_cmd"]
    assert codex_cmd[-1] == ""
    assert calls[0]["env"]["AGENT_MAILBOX_CODEX_EXE"] == str(tmp_path / "codex.exe")
    assert not any("read mailbox task" in part for part in codex_cmd)


def test_launch_tui_chat_uses_existing_window_id(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    workspace = f"agent-mailbox-{task_id}"
    calls = []

    def fake_launch_workspace(**kwargs):
        return {"workspace": workspace, "claude_pane_id": 11, "codex_pane_id": 12, "spawned_at": "now"}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"pane_id": 11, "workspace": workspace, "window_id": 99}]), "")
        if "spawn" in argv:
            return subprocess.CompletedProcess(argv, 0, "44\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr("tui_launcher.find_codex", lambda: None)
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": True,
            "visible": True,
            "agents": {"claude": {"state": "ready"}, "codex": {"state": "ready"}},
        },
    )
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda *args, **kwargs: None)
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})
    monkeypatch.setattr("subprocess.run", fake_run)

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json"])
    assert mailbox_cli.cmd_launch_tui(args) == 0
    spawn_call = next(call for call in calls if "spawn" in call)
    assert "--window-id" in spawn_call
    assert "99" in spawn_call
    assert "--workspace" not in spawn_call
    assert "--new-window" not in spawn_call
    conn = connect_db(root)
    chat = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='chat'", (task_id,)).fetchone()
    assert chat["pane_id"] == 44
    control = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='control'", (task_id,)).fetchone()
    assert control["pane_id"] == 44


def test_launch_tui_attaches_visible_gui_after_panes(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    workspace = f"agent-mailbox-{task_id}"
    attach_calls = []

    def fake_launch_workspace(**kwargs):
        return {"workspace": workspace, "claude_pane_id": 11, "codex_pane_id": 12, "spawned_at": "now"}

    def fake_run(argv, **kwargs):
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"pane_id": 11, "workspace": workspace, "window_id": 99}]), "")
        if "spawn" in argv:
            return subprocess.CompletedProcess(argv, 0, "44\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr("tui_launcher.find_codex", lambda: None)
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": True,
            "visible": True,
            "agents": {"claude": {"state": "ready"}, "codex": {"state": "ready"}},
        },
    )
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda wez, ws, cwd: attach_calls.append((wez, ws, cwd)))
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})
    monkeypatch.setattr("subprocess.run", fake_run)

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json"])
    assert mailbox_cli.cmd_launch_tui(args) == 0
    assert attach_calls == [(tmp_path / "wezterm.exe", workspace, tmp_path)]


def test_startup_gate_pauses_relay_when_agent_pane_not_ready(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    split_calls = []
    launched_workspace = None

    def fake_launch_workspace(**kwargs):
        nonlocal launched_workspace
        launched_workspace = kwargs["workspace"]
        return {"workspace": kwargs["workspace"], "claude_pane_id": 11, "codex_pane_id": 12, "spawned_at": "now"}

    def fake_run(argv, **kwargs):
        if "split-pane" in argv:
            split_calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"pane_id": 11, "workspace": launched_workspace or "w", "window_id": 99}]), "")
        if "spawn" in argv:
            return subprocess.CompletedProcess(argv, 0, "44\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr("tui_launcher.find_codex", lambda: None)
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": False,
            "visible": True,
            "agents": {"claude": {"state": "ready"}, "codex": {"state": "update_prompt"}},
        },
    )
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda *args, **kwargs: None)
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})
    monkeypatch.setattr("subprocess.run", fake_run)

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["start", "--root", str(root), "--prefix", "t2", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json"])
    assert mailbox_cli.cmd_start(args) == 0
    conn = connect_db(root)
    task_id = conn.execute("SELECT id FROM rooms").fetchone()["id"]
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id=?", (task_id,)).fetchone()
    assert relay["paused"] == 1
    assert relay["pause_reason"] == "startup_not_ready:claude=ready,codex=update_prompt"
    assert not any("launch_relay_pane.cmd" in " ".join(call) for call in split_calls)


def test_start_warns_about_stale_workspaces_without_reaping(monkeypatch, tmp_path, capsys):
    root = tmp_path / "mb"
    launched_workspace = None
    killed = []

    def fake_launch_workspace(**kwargs):
        nonlocal launched_workspace
        launched_workspace = kwargs["workspace"]
        return {"workspace": kwargs["workspace"], "claude_pane_id": 11, "codex_pane_id": 12, "spawned_at": "now"}

    def fake_run(argv, **kwargs):
        if "list" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {"pane_id": 91, "workspace": "agent-mailbox-orphan", "window_id": 99},
                        {"pane_id": 11, "workspace": launched_workspace or "agent-mailbox-active", "window_id": 99},
                    ]
                ),
                "",
            )
        if "kill-pane" in argv:
            killed.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_wezterm_cli(argv, **kwargs):
        if "list" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {"pane_id": 91, "workspace": "agent-mailbox-orphan", "window_id": 99},
                        {"pane_id": 11, "workspace": launched_workspace or "agent-mailbox-active", "window_id": 99},
                    ]
                ),
                "",
            )
        if "kill-pane" in argv:
            killed.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr("tui_launcher.find_codex", lambda: None)
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr(
        "tui_launcher.validate_workspace_startup",
        lambda **kwargs: {
            "ready": False,
            "visible": True,
            "agents": {"claude": {"state": "ready"}, "codex": {"state": "update_prompt"}},
        },
    )
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda *args, **kwargs: None)
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("tui_launcher.run_wezterm_cli", fake_wezterm_cli)

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(
        ["start", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--no-chat", "--no-control-panel", "--format", "json"]
    )
    assert mailbox_cli.cmd_start(args) == 0
    captured = capsys.readouterr()
    assert "stale agent-mailbox workspaces detected" in captured.err
    assert "agent-mailbox-orphan" in captured.err
    assert killed == []


def test_start_emits_single_json_and_binds_relay_fake_pane(tmp_path):
    root = tmp_path / "mb"
    env = dict(
        **__import__("os").environ,
        AGENT_MAILBOX_FAKE_PANE_IDS="21,22,23,24,25",
        AGENT_MAILBOX_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"),
    )
    rv = _run(
        "start",
        "--root",
        str(root),
        "--prefix",
        "t",
        "--goal",
        "g",
        "--project-cwd",
        str(tmp_path),
        "--max-iters",
        "3",
        "--format",
        "json",
        env=env,
    )
    assert rv.returncode == 0, rv.stderr
    lines = [line for line in rv.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    task_id = json.loads(lines[0])["data"]["task_id"]
    conn = connect_db(root)
    relay = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='relay'", (task_id,)).fetchone()
    assert relay["pane_id"] == 23
    chat = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='chat'", (task_id,)).fetchone()
    assert chat["pane_id"] == 24
    control = conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='control'", (task_id,)).fetchone()
    assert control["pane_id"] == 25


def test_start_no_chat_disables_chat_fake_pane(tmp_path):
    root = tmp_path / "mb"
    env = dict(
        **__import__("os").environ,
        AGENT_MAILBOX_FAKE_PANE_IDS="21,22,23,24,25",
        AGENT_MAILBOX_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"),
    )
    rv = _run(
        "start",
        "--root",
        str(root),
        "--prefix",
        "t",
        "--goal",
        "g",
        "--project-cwd",
        str(tmp_path),
        "--no-chat",
        "--no-control-panel",
        "--format",
        "json",
        env=env,
    )
    assert rv.returncode == 0, rv.stderr
    task_id = json.loads(rv.stdout)["data"]["task_id"]
    conn = connect_db(root)
    panes = {row["pane_role"]: row["pane_id"] for row in conn.execute("SELECT * FROM panes WHERE room_id=?", (task_id,))}
    assert panes["relay"] == 23
    assert "chat" not in panes
    assert "control" not in panes


def test_watch_chat_emits_existing_messages_without_mutating_state(tmp_path):
    root = tmp_path / "mb"
    init = _run(
        "init",
        "--root",
        str(root),
        "--prefix",
        "t",
        "--goal",
        "g",
        "--project-cwd",
        str(tmp_path),
        "--context",
        "bootstrap body",
        "--format",
        "json",
    )
    task_id = json.loads(init.stdout)["data"]["task_id"]
    _seed_message(root, task_id, from_agent="claude", to_agent="codex", status="continue", summary="hello", body="body")
    conn = connect_db(root)
    before_room = dict(conn.execute("SELECT status, turn, last_message_id, round FROM rooms WHERE id=?", (task_id,)).fetchone())
    before_messages = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE room_id=?", (task_id,)).fetchone()["n"]
    conn.close()
    rv = _run("watch-chat", "--root", str(root), "--task-id", task_id, "--max-iters", "1", "--no-color")
    assert rv.returncode == 0, rv.stderr
    assert "read-only transcript view" in rv.stdout
    assert "[1]" in rv.stdout
    assert "user -> broadcast [continue] bootstrap context" in rv.stdout
    assert "claude -> codex [continue] hello" in rv.stdout
    assert "body" in rv.stdout
    conn = connect_db(root)
    after_room = dict(conn.execute("SELECT status, turn, last_message_id, round FROM rooms WHERE id=?", (task_id,)).fetchone())
    after_messages = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE room_id=?", (task_id,)).fetchone()["n"]
    conn.close()
    assert after_room == before_room
    assert after_messages == before_messages


def test_watch_chat_from_message_id_emits_only_newer_messages(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    _seed_message(root, task_id, from_agent="claude", to_agent="codex", status="continue", summary="old", body="old body")
    _seed_message(root, task_id, from_agent="codex", to_agent="claude", status="continue", summary="new", body="new body")
    rv = _run("watch-chat", "--root", str(root), "--task-id", task_id, "--from-message-id", "1", "--max-iters", "1", "--no-color")
    assert rv.returncode == 0, rv.stderr
    assert "read-only transcript view" in rv.stdout
    assert "old body" not in rv.stdout
    assert "codex -> claude [continue] new" in rv.stdout
    assert "new body" in rv.stdout


def test_dry_run_prints_current_turn_doorbell_without_mutating(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    conn = connect_db(root)
    before = dict(conn.execute("SELECT turn, round, last_message_id FROM rooms WHERE id=?", (task_id,)).fetchone())
    conn.close()
    rv = _run("dry-run", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert rv.returncode == 0, rv.stderr
    data = json.loads(rv.stdout)["data"]
    assert data["agent"] == "claude"
    assert data["peer"] == "codex"
    assert f"agent-mailbox task {task_id}" in data["doorbell_text"]
    assert "write your reply to:" in data["doorbell_text"]
    assert "from: claude" in data["doorbell_text"]
    assert "to: codex" in data["doorbell_text"]
    conn = connect_db(root)
    after = dict(conn.execute("SELECT turn, round, last_message_id FROM rooms WHERE id=?", (task_id,)).fetchone())
    conn.close()
    assert after == before


def test_control_panel_parser_defaults_and_once_status(tmp_path):
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(
        [
            "start",
            "--root",
            str(tmp_path / "mb"),
            "--prefix",
            "t",
            "--goal",
            "g",
            "--project-cwd",
            str(tmp_path),
        ]
    )
    assert args.with_control_panel is True
    no_panel = mailbox_cli.build_parser().parse_args(
        [
            "launch-tui",
            "--root",
            str(tmp_path / "mb"),
            "--task-id",
            "t1",
            "--no-control-panel",
        ]
    )
    assert no_panel.with_control_panel is False

    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    rv = _run("control-panel", "--root", str(root), "--task-id", task_id, "--once")
    assert rv.returncode == 0, rv.stderr
    assert "Agent Mailbox Control Panel" in rv.stdout
    assert f"Task: {task_id}" in rv.stdout
    assert f"Root: {root}" in rv.stdout


def test_control_panel_actions_use_mailbox_subprocess(monkeypatch, tmp_path):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    args = mailbox_cli.build_parser().parse_args(["control-panel", "--root", str(root), "--task-id", "t1", "--commands", "q"])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}\n', "")

    monkeypatch.setattr(mailbox_cli.subprocess, "run", fake_run)
    assert mailbox_cli._panel_pause(args, reason="test_reason")
    assert mailbox_cli._panel_resume(args)
    assert mailbox_cli._panel_inject(args, "hello")
    assert mailbox_cli._panel_stop(args)
    assert mailbox_cli._panel_rediscover_codex(args)
    assert mailbox_cli._panel_restart_agent(args, "claude")

    joined = [" ".join(call) for call in calls]
    assert any("pause" in call and "--reason test_reason" in call for call in joined)
    assert any("resume" in call for call in joined)
    assert any("inject" in call and "--content hello" in call for call in joined)
    assert any("stop" in call for call in joined)
    assert any("repair" in call and "--rediscover-codex" in call for call in joined)
    assert any("repair" in call and "--restart-agent claude" in call for call in joined)
    assert not any("--close-panes" in call for call in joined)


def test_control_panel_bounce_claude_pauses_restarts_and_resumes(monkeypatch, tmp_path):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    args = mailbox_cli.build_parser().parse_args(["control-panel", "--root", str(root), "--task-id", task_id])
    calls = []
    answers = iter(["", "y", ""])

    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(mailbox_cli, "_panel_cli", lambda _args, *subargs: calls.append(subargs) or True)

    mailbox_cli._panel_bounce_agent(args, "claude")
    assert calls == [
        ("pause", "--reason", "control_panel_bounce_claude"),
        ("repair", "--restart-agent", "claude"),
        ("resume",),
    ]


def test_control_panel_codex_bounce_without_session_offers_rediscovery(monkeypatch, tmp_path):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    args = mailbox_cli.build_parser().parse_args(["control-panel", "--root", str(root), "--task-id", task_id])
    calls = []
    states = [
        {
            "task_id": task_id,
            "room": {"status": "waiting", "workspace": "w"},
            "relay": {},
            "sessions": {"codex": {"session_id": None, "discovery_status": "pending"}},
            "panes": {},
            "latest": None,
        },
        {
            "task_id": task_id,
            "room": {"status": "waiting", "workspace": "w"},
            "relay": {},
            "sessions": {"codex": {"session_id": "codex-known", "discovery_status": "discovered"}},
            "panes": {},
            "latest": None,
        },
    ]
    answers = iter(["y", "", "y", ""])

    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(mailbox_cli, "_read_panel_state", lambda _root, _task_id: states.pop(0))
    monkeypatch.setattr(mailbox_cli, "_panel_cli", lambda _args, *subargs: calls.append(subargs) or True)

    mailbox_cli._panel_bounce_agent(args, "codex")
    assert calls == [
        ("repair", "--rediscover-codex"),
        ("pause", "--reason", "control_panel_bounce_codex"),
        ("repair", "--restart-agent", "codex"),
        ("resume",),
    ]


def test_control_panel_rebind_validates_live_workspace_pane(monkeypatch, tmp_path):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    args = mailbox_cli.build_parser().parse_args(["control-panel", "--root", str(root), "--task-id", task_id])
    calls = []
    answers = iter(["claude", "777", "y"])

    monkeypatch.setattr(mailbox_cli, "_live_workspace_panes", lambda _root, _task_id: [{"pane_id": 777, "title": "Claude"}])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(mailbox_cli, "_panel_cli", lambda _args, *subargs: calls.append(subargs) or True)

    mailbox_cli._panel_rebind_pane_interactive(args)
    assert calls == [("repair", "--rebind-pane", "--agent", "claude", "--pane-id", "777")]


def test_control_panel_rebind_refuses_non_live_pane(monkeypatch, tmp_path):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    args = mailbox_cli.build_parser().parse_args(["control-panel", "--root", str(root), "--task-id", task_id])
    calls = []
    answers = iter(["claude", "999"])

    monkeypatch.setattr(mailbox_cli, "_live_workspace_panes", lambda _root, _task_id: [{"pane_id": 777, "title": "Claude"}])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(mailbox_cli, "_panel_cli", lambda _args, *subargs: calls.append(subargs) or True)

    mailbox_cli._panel_rebind_pane_interactive(args)
    assert calls == []


def test_watch_chat_terminal_room_exits_after_grace(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    _seed_message(root, task_id, from_agent="claude", to_agent="codex", status="final", summary="done", body="done")
    rv = _run(
        "watch-chat",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--poll-interval-s",
        "0.01",
        "--terminal-grace-s",
        "0.01",
        "--no-color",
    )
    assert rv.returncode == 0, rv.stderr
    assert "[agent-mailbox] room terminal: final" in rv.stdout


def test_launch_chat_pane_cmd_shape():
    text = (SKILL_ROOT / "scripts" / "launch_chat_pane.cmd").read_text(encoding="utf-8")
    assert "watch-chat --root" in text
    assert "AGENT_MAILBOX_TASK_ID" in text
    assert "AGENT_MAILBOX_ROOT" in text
    assert "cmd /k" in text


def test_launch_control_panel_cmd_shape():
    text = (SKILL_ROOT / "scripts" / "launch_control_panel.cmd").read_text(encoding="utf-8")
    assert "control-panel --root" in text
    assert "AGENT_MAILBOX_TASK_ID" in text
    assert "AGENT_MAILBOX_ROOT" in text
    assert "role=control" in text
    assert "cmd /k" in text


def test_agent_pane_wrappers_call_nested_cmd_shims():
    codex_text = (SKILL_ROOT / "scripts" / "launch_codex_pane.cmd").read_text(encoding="utf-8")
    claude_text = (SKILL_ROOT / "scripts" / "launch_claude_pane.cmd").read_text(encoding="utf-8")
    codex_resume_text = (SKILL_ROOT / "scripts" / "resume_codex_pane.cmd").read_text(encoding="utf-8")
    claude_resume_text = (SKILL_ROOT / "scripts" / "resume_claude_pane.cmd").read_text(encoding="utf-8")
    assert "call codex" in codex_text
    assert "call claude" in claude_text
    assert "AGENT_MAILBOX_CODEX_EXE" in codex_text
    assert "--ask-for-approval" in codex_text
    assert "AGENT_MAILBOX_CODEX_APPROVAL=never" in codex_text
    assert "AGENT_MAILBOX_CODEX_SANDBOX=danger-full-access" in codex_text
    assert "--ask-for-approval" in codex_resume_text
    assert "--permission-mode" in claude_text
    assert "AGENT_MAILBOX_CLAUDE_PERMISSION_MODE=auto" in claude_text
    assert "--permission-mode" in claude_resume_text
    assert "cmd /k" in codex_text
    assert "cmd /k" in claude_text


def test_pause_stop_and_repair_rebind(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    assert _run("pause", "--root", str(root), "--task-id", task_id).returncode == 0
    assert _run("repair", "--root", str(root), "--task-id", task_id, "--rebind-pane", "--agent", "claude", "--pane-id", "123").returncode == 0
    assert _run("stop", "--root", str(root), "--task-id", task_id).returncode == 0
    conn = connect_db(root)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (task_id,)).fetchone()["status"] == "stopped"


def test_stop_close_panes_kills_task_scoped_processes(monkeypatch, tmp_path):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    project = tmp_path / "project"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(project), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    conn = connect_db(root)
    conn.execute("INSERT OR REPLACE INTO panes(room_id, pane_role, pane_id) VALUES(?, 'claude', 123)", (task_id,))
    room = conn.execute("SELECT workspace FROM rooms WHERE id=?", (task_id,)).fetchone()
    workspace = room["workspace"]
    conn.close()
    killed_processes = []
    killed_panes = []
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr(
        "tui_launcher.run_wezterm_cli",
        lambda argv, **kwargs: killed_panes.append(int(argv[argv.index("--pane-id") + 1])) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(mailbox_cli.os, "name", "nt")
    monkeypatch.setattr(mailbox_cli.os, "getpid", lambda: 999)
    monkeypatch.setattr(mailbox_cli, "_run_taskkill_tree", lambda pid: killed_processes.append(pid) or True)

    rows = json.dumps(
        [
            {"ProcessId": 111, "CommandLine": f"cmd /k launch_control_panel {workspace}"},
            {"ProcessId": 222, "CommandLine": "cmd /k unrelated"},
            {"ProcessId": 999, "CommandLine": f"python mailbox.py stop --root {root}"},
        ]
    )

    def fake_run(argv, **kwargs):
        if argv[:3] == ["powershell", "-NoProfile", "-Command"]:
            return subprocess.CompletedProcess(argv, 0, rows, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    args = mailbox_cli.build_parser().parse_args(["stop", "--root", str(root), "--task-id", task_id, "--close-panes", "--yes", "--format", "json"])
    assert mailbox_cli.cmd_stop(args) == 0
    assert killed_panes == [123]
    assert killed_processes == [111]


def test_task_scoped_cleanup_excludes_current_process_ancestors(monkeypatch):
    import mailbox as mailbox_cli

    monkeypatch.setattr(mailbox_cli.os, "getpid", lambda: 300)
    killed = []
    monkeypatch.setattr(mailbox_cli, "_run_taskkill_tree", lambda pid: killed.append(pid) or True)
    processes = [
        {"pid": 100, "parent_pid": 0, "name": "powershell.exe", "command_line": "powershell stop --task-id task-1"},
        {"pid": 200, "parent_pid": 100, "name": "python.exe", "command_line": "python mailbox.py stop --task-id task-1"},
        {"pid": 300, "parent_pid": 200, "name": "python.exe", "command_line": "pytest task-1"},
        {"pid": 400, "parent_pid": 0, "name": "wezterm.exe", "command_line": "wezterm start --workspace agent-mailbox-task-1"},
    ]
    assert mailbox_cli._kill_task_scoped_pids(processes, tokens={"task-1", "agent-mailbox-task-1"}) == [400]
    assert killed == [400]


def test_status_detects_dead_watcher_in_running_state(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    conn = connect_db(root)
    conn.execute("UPDATE tui_relay_state SET watcher_pid=999999999, paused=0, pause_reason=NULL WHERE room_id=?", (task_id,))
    conn.close()

    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert status.returncode == 0, status.stderr
    data = json.loads(status.stdout)["data"]
    assert data["watcher_pid"] == 999999999
    assert data["watcher_alive"] is False
    assert data["watcher_dead_with_running_state"] is True


def test_status_reports_pending_user_injections(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    inject = _run("inject", "--root", str(root), "--task-id", task_id, "--content", "please inspect", "--format", "json")
    assert inject.returncode == 0, inject.stderr
    message_id = json.loads(inject.stdout)["data"]["message_id"]

    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["pending_user_injections"] == 1

    ack = _run("ack", "--root", str(root), "--task-id", task_id, "--agent", "claude", "--message-id", str(message_id), "--format", "json")
    assert ack.returncode == 0, ack.stderr
    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["pending_user_injections"] == 0


def test_bootstrap_context_does_not_count_as_pending_user_injection(tmp_path):
    root = tmp_path / "mb"
    init = _run(
        "init",
        "--root",
        str(root),
        "--prefix",
        "t",
        "--goal",
        "g",
        "--project-cwd",
        str(tmp_path),
        "--context",
        "bootstrap context should not count as an injection",
        "--format",
        "json",
    )
    task_id = json.loads(init.stdout)["data"]["task_id"]

    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["pending_user_injections"] == 0
    panel = _run("control-panel", "--root", str(root), "--task-id", task_id, "--once")
    assert "Pending user injections: 0" in panel.stdout

    inject = _run("inject", "--root", str(root), "--task-id", task_id, "--content", "real guidance", "--format", "json")
    assert inject.returncode == 0, inject.stderr
    message_id = json.loads(inject.stdout)["data"]["message_id"]
    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["pending_user_injections"] == 1
    ack = _run("ack", "--root", str(root), "--task-id", task_id, "--agent", "claude", "--message-id", str(message_id), "--format", "json")
    assert ack.returncode == 0, ack.stderr
    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["pending_user_injections"] == 0


def test_reap_stale_workspaces_dry_run_and_yes(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    active_task = json.loads(init.stdout)["data"]["task_id"]
    stopped = _run("init", "--root", str(root), "--prefix", "done", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    stopped_task = json.loads(stopped.stdout)["data"]["task_id"]
    conn = connect_db(root)
    conn.execute("UPDATE rooms SET status='stopped' WHERE id=?", (stopped_task,))
    conn.close()
    panes = [
        {"pane_id": 11, "workspace": f"agent-mailbox-{active_task}"},
        {"pane_id": 22, "workspace": f"agent-mailbox-{stopped_task}"},
        {"pane_id": 33, "workspace": "agent-mailbox-orphan"},
        {"pane_id": 44, "workspace": "default"},
    ]
    calls = []
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(panes), "")
        if "kill-pane" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_wezterm_cli(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(panes), "")
        if "kill-pane" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("tui_launcher.run_wezterm_cli", fake_wezterm_cli)
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["reap-stale-workspaces", "--root", str(root), "--format", "json"])
    assert mailbox_cli.cmd_reap_stale_workspaces(args) == 0
    assert not any("kill-pane" in call for call in calls)

    args = mailbox_cli.build_parser().parse_args(["reap-stale-workspaces", "--root", str(root), "--yes", "--format", "json"])
    assert mailbox_cli.cmd_reap_stale_workspaces(args) == 0
    killed = [
        call[call.index("--pane-id") + 1]
        for call in calls
        if "kill-pane" in call and "--pane-id" in call
    ]
    assert "22" in killed
    assert "33" in killed
    assert "11" not in killed


def test_reap_stale_workspaces_tolerates_wezterm_list_timeout(monkeypatch, tmp_path, capsys):
    root = tmp_path / "mb"
    _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    monkeypatch.setattr(
        "tui_launcher.run_wezterm_cli",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 124, "", "wezterm cli timed out"),
    )
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["reap-stale-workspaces", "--root", str(root), "--yes", "--format", "json"])
    assert mailbox_cli.cmd_reap_stale_workspaces(args) == 0
    captured = capsys.readouterr()
    assert "unable to list WezTerm panes" in captured.err
    assert '"stale_workspaces": []' in captured.out


def test_doctor_wezterm_enumerates_processes_and_workspaces(monkeypatch, tmp_path, capsys):
    import mailbox as mailbox_cli

    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    conn = connect_db(root)
    workspace = conn.execute("SELECT workspace FROM rooms WHERE id=?", (task_id,)).fetchone()["workspace"]
    conn.close()
    monkeypatch.setattr(mailbox_cli, "_list_processes", lambda: [
        {"pid": 1, "name": "wezterm-mux-server.exe", "command_line": "mux"},
        {"pid": 2, "name": "wezterm.exe", "command_line": "wezterm cli --prefer-mux list --format json"},
    ])
    monkeypatch.setattr(mailbox_cli, "_wezterm_mux_panes", lambda wez: ([{"pane_id": 11, "workspace": workspace}, {"pane_id": 12, "workspace": "default"}], None))
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")

    args = mailbox_cli.build_parser().parse_args(["doctor-wezterm", "--root", str(root), "--format", "json"])
    assert mailbox_cli.cmd_doctor_wezterm(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["mux_responsive"] is True
    assert data["mux_cli_helper_count"] == 1
    assert data["agent_mailbox_workspaces"][0]["workspace"] == workspace
    assert data["agent_mailbox_workspaces"][0]["room"]["status"] == "waiting"


def test_doctor_wezterm_falls_back_when_mux_unresponsive(monkeypatch, tmp_path, capsys):
    import mailbox as mailbox_cli

    monkeypatch.setattr(mailbox_cli, "_list_processes", lambda: [{"pid": 1, "name": "wezterm.exe", "command_line": "wezterm cli --prefer-mux list"}])
    monkeypatch.setattr(mailbox_cli, "_wezterm_mux_panes", lambda wez: ([], "wezterm cli timed out"))
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: tmp_path / "wezterm.exe")
    args = mailbox_cli.build_parser().parse_args(["doctor-wezterm", "--root", str(tmp_path / "mb"), "--format", "json"])
    assert mailbox_cli.cmd_doctor_wezterm(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["mux_responsive"] is False
    assert data["mux_error"] == "wezterm cli timed out"
    assert data["mux_cli_helper_count"] == 1


def test_reset_wezterm_requires_scope_flag(tmp_path):
    rv = _run("reset-wezterm", "--root", str(tmp_path / "mb"), "--format", "json")
    assert rv.returncode == 2
    assert "choose exactly one scope" in json.loads(rv.stdout)["error"]


def test_reset_wezterm_dry_run_kills_nothing(monkeypatch, tmp_path, capsys):
    import mailbox as mailbox_cli

    killed = []
    monkeypatch.setattr(mailbox_cli.os, "getpid", lambda: 999)
    monkeypatch.setattr(mailbox_cli, "_run_taskkill_tree", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(mailbox_cli, "_list_processes", lambda: [
        {"pid": 111, "name": "wezterm.exe", "command_line": "wezterm start --workspace agent-mailbox-t1"},
    ])
    args = mailbox_cli.build_parser().parse_args(["reset-wezterm", "--root", str(tmp_path / "mb"), "--task-scoped", "--dry-run", "--format", "json"])
    assert mailbox_cli.cmd_reset_wezterm(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["planned"][0]["pid"] == 111
    assert data["killed_process_ids"] == []
    assert killed == []


def test_reset_wezterm_task_scoped_excludes_global_mux(monkeypatch, tmp_path, capsys):
    import mailbox as mailbox_cli

    monkeypatch.setattr(mailbox_cli, "_list_processes", lambda: [
        {"pid": 1, "name": "wezterm-mux-server.exe", "command_line": "wezterm-mux-server.exe"},
        {"pid": 2, "name": "wezterm.exe", "command_line": "wezterm start --workspace agent-mailbox-t1"},
    ])
    args = mailbox_cli.build_parser().parse_args(["reset-wezterm", "--root", str(tmp_path / "mb"), "--task-scoped", "--dry-run", "--format", "json"])
    assert mailbox_cli.cmd_reset_wezterm(args) == 0
    planned = json.loads(capsys.readouterr().out)["data"]["planned"]
    assert [p["pid"] for p in planned] == [2]


def test_reset_wezterm_global_requires_yes_global(monkeypatch, tmp_path, capsys):
    import mailbox as mailbox_cli

    monkeypatch.setattr(mailbox_cli, "_list_processes", lambda: [{"pid": 1, "name": "wezterm-mux-server.exe", "command_line": "mux"}])
    args = mailbox_cli.build_parser().parse_args(["reset-wezterm", "--root", str(tmp_path / "mb"), "--global", "--dry-run", "--format", "json"])
    assert mailbox_cli.cmd_reset_wezterm(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["planned"][0]["pid"] == 1
    assert data["killed_process_ids"] == []
    args = mailbox_cli.build_parser().parse_args(["reset-wezterm", "--root", str(tmp_path / "mb"), "--global", "--yes", "--format", "json"])
    assert mailbox_cli.cmd_reset_wezterm(args) == 2
    assert "--yes-global" in json.loads(capsys.readouterr().out)["error"]
    killed = []
    monkeypatch.setattr(mailbox_cli, "_run_taskkill_tree", lambda pid: killed.append(pid) or True)
    args = mailbox_cli.build_parser().parse_args(["reset-wezterm", "--root", str(tmp_path / "mb"), "--global", "--yes-global", "--format", "json"])
    assert mailbox_cli.cmd_reset_wezterm(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["killed_process_ids"] == [1]
    assert killed == [1]


def test_archive_terminal_task_removes_live_rows_and_writes_archive(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    conn = connect_db(root)
    send_message(
        conn,
        root=root,
        room_id=task_id,
        from_agent="claude",
        to_agent="codex",
        kind="message",
        status="final",
        summary="done",
        body="archive me",
    )
    conn.close()

    archive = _run("archive", "--root", str(root), "--task-id", task_id, "--yes", "--format", "json")
    assert archive.returncode == 0, archive.stderr
    data = json.loads(archive.stdout)["data"]
    assert Path(data["archive_db"]).is_file()
    assert Path(data["transcript"]).is_file()
    conn = connect_db(root)
    assert conn.execute("SELECT id FROM rooms WHERE id=?", (task_id,)).fetchone() is None
    import sqlite3

    archived = sqlite3.connect(data["archive_db"])
    archived.row_factory = sqlite3.Row
    try:
        assert archived.execute("SELECT id FROM rooms WHERE id=?", (task_id,)).fetchone() is not None
        msg = archived.execute("SELECT body_text FROM messages WHERE room_id=?", (task_id,)).fetchone()
        assert msg["body_text"] == "archive me"
    finally:
        archived.close()


def test_archive_rmtree_failure_keeps_live_rows(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "artifact.txt").write_text("locked", encoding="utf-8")
    conn = connect_db(root)
    send_message(
        conn,
        root=root,
        room_id=task_id,
        from_agent="claude",
        to_agent="codex",
        kind="message",
        status="final",
        summary="done",
        body="archive me",
    )
    conn.close()

    import mailbox as mailbox_cli

    real_rmtree = mailbox_cli.shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path) == task_dir:
            raise OSError("simulated file lock")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(mailbox_cli.shutil, "rmtree", flaky_rmtree)
    args = mailbox_cli.build_parser().parse_args(["archive", "--root", str(root), "--task-id", task_id, "--yes", "--format", "json"])
    with pytest.raises(OSError, match="simulated file lock"):
        mailbox_cli.cmd_archive(args)
    conn = connect_db(root)
    assert conn.execute("SELECT id FROM rooms WHERE id=?", (task_id,)).fetchone() is not None
    assert task_dir.exists()


def test_repair_restart_agent_sends_resume_to_bound_pane(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    assert _run("repair", "--root", str(root), "--task-id", task_id, "--rebind-pane", "--agent", "codex", "--pane-id", "321").returncode == 0
    conn = connect_db(root)
    conn.execute("UPDATE agent_sessions SET session_id='codex-known', discovery_status='discovered' WHERE room_id=? AND agent='codex'", (task_id,))
    conn.close()
    calls = []
    wez = tmp_path / "wezterm.exe"
    wez.write_bytes(b"")
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: wez)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(
        ["repair", "--root", str(root), "--task-id", task_id, "--restart-agent", "codex", "--format", "json"]
    )
    assert mailbox_cli.cmd_repair(args) == 0
    sent = " ".join(calls[0])
    assert "send-text" in sent
    assert "321" in sent
    assert "codex resume" in sent


def test_list_initializes_empty_db(tmp_path):
    rv = _run("list", "--root", str(tmp_path / "mb"), "--format", "json")
    assert rv.returncode == 0
    assert json.loads(rv.stdout)["data"]["rooms"] == []
