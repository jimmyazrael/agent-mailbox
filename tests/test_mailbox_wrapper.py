import json
import subprocess
import sys
from pathlib import Path

from agent_chat import connect_db, init_db

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX = SKILL_ROOT / "scripts" / "mailbox.py"


def _run(*args, env=None):
    return subprocess.run([sys.executable, str(MAILBOX), *args], capture_output=True, text=True, env=env)


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


def test_post_show_ack_export_and_status(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "hello",
        "--body",
        "body",
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    show = _run("show", "--root", str(root), "--task-id", task_id, "--body", "--format", "json")
    assert json.loads(show.stdout)["data"]["messages"][0]["body"] == "body"
    status = _run("status", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert json.loads(status.stdout)["data"]["turn"] == "codex"
    export = _run("export", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert Path(json.loads(export.stdout)["data"]["path"]).exists()


def test_post_warns_when_non_terminal_protocol_header_missing(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "agree",
        "--body",
        "I agree we should run it.",
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    warnings = json.loads(post.stdout)["data"]["warnings"]
    assert "missing protocol header: Mode" in warnings
    assert "non-terminal posts must name a concrete Next action or Blocked on" in warnings


def test_post_accepts_role_mode_protocol_header(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
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
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "execute",
        "--body",
        body,
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    assert json.loads(post.stdout)["data"]["warnings"] == []


def test_post_accepts_lowercase_protocol_mode(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
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
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "discuss",
        "--body",
        body,
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    assert json.loads(post.stdout)["data"]["warnings"] == []


def test_post_accepts_v11_synonym_protocol_mode(tmp_path):
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
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "coordinate",
        "--body",
        body,
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    assert json.loads(post.stdout)["data"]["warnings"] == []


def test_post_rejects_arbitrary_protocol_mode(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
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
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "bad mode",
        "--body",
        body,
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    assert "unknown protocol Mode: lol" in json.loads(post.stdout)["data"]["warnings"]


def test_post_rejects_empty_protocol_mode(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
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
    post = _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "empty mode",
        "--body",
        body,
        "--format",
        "json",
    )
    assert post.returncode == 0, post.stderr
    assert "missing protocol header: Mode" in json.loads(post.stdout)["data"]["warnings"]


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
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda *args, **kwargs: None)
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["launch-tui", "--root", str(root), "--task-id", task_id, "--no-chat", "--no-control-panel", "--format", "json"])
    assert mailbox_cli.cmd_launch_tui(args) == 0
    codex_cmd = calls[0]["codex_cmd"]
    assert codex_cmd[-1] == ""
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
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
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
    monkeypatch.setattr("tui_launcher.ensure_mux_alive", lambda *args, **kwargs: None)
    monkeypatch.setattr("tui_launcher.launch_workspace", fake_launch_workspace)
    monkeypatch.setattr("tui_launcher.attach_workspace_gui", lambda wez, ws, cwd: attach_calls.append((wez, ws, cwd)))
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})
    monkeypatch.setattr("subprocess.run", fake_run)

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json"])
    assert mailbox_cli.cmd_launch_tui(args) == 0
    assert attach_calls == [(tmp_path / "wezterm.exe", workspace, tmp_path)]


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
    assert _run(
        "post",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--from",
        "claude",
        "--to",
        "codex",
        "--status",
        "continue",
        "--summary",
        "hello",
        "--body",
        "body",
    ).returncode == 0
    conn = connect_db(root)
    before_room = dict(conn.execute("SELECT status, turn, last_message_id, round FROM rooms WHERE id=?", (task_id,)).fetchone())
    before_messages = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE room_id=?", (task_id,)).fetchone()["n"]
    conn.close()
    rv = _run("watch-chat", "--root", str(root), "--task-id", task_id, "--max-iters", "1", "--no-color")
    assert rv.returncode == 0, rv.stderr
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
    _run("post", "--root", str(root), "--task-id", task_id, "--from", "claude", "--to", "codex", "--status", "continue", "--summary", "old", "--body", "old body")
    _run("post", "--root", str(root), "--task-id", task_id, "--from", "codex", "--to", "claude", "--status", "continue", "--summary", "new", "--body", "new body")
    rv = _run("watch-chat", "--root", str(root), "--task-id", task_id, "--from-message-id", "1", "--max-iters", "1", "--no-color")
    assert rv.returncode == 0, rv.stderr
    assert "old body" not in rv.stdout
    assert "codex -> claude [continue] new" in rv.stdout
    assert "new body" in rv.stdout


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
    _run("post", "--root", str(root), "--task-id", task_id, "--from", "claude", "--to", "codex", "--status", "final", "--summary", "done", "--body", "done")
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


def test_pause_stop_and_repair_rebind(tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    assert _run("pause", "--root", str(root), "--task-id", task_id).returncode == 0
    assert _run("repair", "--root", str(root), "--task-id", task_id, "--rebind-pane", "--agent", "claude", "--pane-id", "123").returncode == 0
    assert _run("stop", "--root", str(root), "--task-id", task_id).returncode == 0
    conn = connect_db(root)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (task_id,)).fetchone()["status"] == "stopped"


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
