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


def test_launch_tui_fake_panes(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init = _run("init", "--root", str(root), "--prefix", "t", "--goal", "g", "--project-cwd", str(tmp_path), "--format", "json")
    task_id = json.loads(init.stdout)["data"]["task_id"]
    env = dict(**__import__("os").environ, AGENT_MAILBOX_FAKE_PANE_IDS="11,12", AGENT_MAILBOX_CODEX_SESSIONS_DIR=str(tmp_path / "sessions"))
    rv = _run("launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json", env=env)
    assert rv.returncode == 0, rv.stderr
    data = json.loads(rv.stdout)["data"]
    assert data["claude_pane_id"] == 11
    conn = connect_db(root)
    assert conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_role='relay'", (task_id,)).fetchone() is None
    relay = conn.execute("SELECT paused, pause_reason FROM tui_relay_state WHERE room_id=?", (task_id,)).fetchone()
    assert relay["paused"] == 0
    assert relay["pause_reason"] is None


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
    monkeypatch.setattr("codex_session_discovery.find_codex_session_id", lambda **kwargs: {"session_id": None, "status": "failed", "scanned_files": 0, "attempted_at": "now"})

    import mailbox as mailbox_cli

    args = mailbox_cli.build_parser().parse_args(["launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json"])
    assert mailbox_cli.cmd_launch_tui(args) == 0
    codex_cmd = calls[0]["codex_cmd"]
    assert codex_cmd[-1] == ""
    assert not any("read mailbox task" in part for part in codex_cmd)


def test_start_emits_single_json_and_binds_relay_fake_pane(tmp_path):
    root = tmp_path / "mb"
    env = dict(
        **__import__("os").environ,
        AGENT_MAILBOX_FAKE_PANE_IDS="21,22,23",
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
