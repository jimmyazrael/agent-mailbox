import json
import subprocess
import sys
from pathlib import Path

import mailbox_eval
from agent_chat import add_participant, connect_db, init_db, init_room, send_message, set_pane

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX_EVAL = SKILL_ROOT / "scripts" / "mailbox_eval.py"
SCENARIOS = SKILL_ROOT / "eval" / "scenarios"


def test_eval_scenarios_are_hard_and_structured():
    files = sorted(SCENARIOS.glob("*.json"))
    assert len(files) >= 5
    categories = set()
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"].startswith("AM-")
        assert data["difficulty"] == "hard"
        assert data["goal"]
        assert data["context"]
        assert data["workspace_files"]
        assert data["success"]["terminal_status"] == "final"
        categories.add(data["category"])
    assert {"context-bootstrap", "blocked-resume", "idempotency", "recovery", "context-overload", "role-mode-protocol"}.issubset(categories)


def test_mailbox_eval_refuses_real_without_env():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-01", "--run-real"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 2
    assert "AGENT_MAILBOX_RUN_REAL_SMOKE=1" in rv.stderr


def test_mailbox_eval_initializes_scenario_without_real_agents():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-01"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 0, rv.stderr
    result = json.loads(rv.stdout)
    assert result["scenario_id"] == "AM-01"
    assert result["status"] == "defined"
    assert result["task_id"]


def test_mailbox_eval_extra_trigger_targets_current_turn(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="w", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    set_pane(conn, "t1", "claude", pane_id=11)
    set_pane(conn, "t1", "codex", pane_id=12)
    send_message(conn, root=root, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="continue", summary="go", body="body")
    conn.close()
    calls = []

    def fake_run_mailbox(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")

    monkeypatch.setattr(mailbox_eval, "_run_mailbox", fake_run_mailbox)
    assert mailbox_eval._send_extra_triggers(root, "t1", 2) is None
    assert calls == [
        ("trigger", "--root", str(root), "--task-id", "t1", "--agent", "codex", "--format", "json"),
        ("trigger", "--root", str(root), "--task-id", "t1", "--agent", "codex", "--format", "json"),
    ]


def test_mailbox_eval_rediscover_action_calls_repair(monkeypatch, tmp_path):
    calls = []

    def fake_run_mailbox(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")

    monkeypatch.setattr(mailbox_eval, "_run_mailbox", fake_run_mailbox)
    assert mailbox_eval._run_action(tmp_path / "mb", "t1", mailbox_eval.ACTION_REDISCOVER_CODEX) is None
    assert calls == [
        ("repair", "--root", str(tmp_path / "mb"), "--task-id", "t1", "--rediscover-codex", "--format", "json")
    ]


def test_mailbox_eval_terminal_room_skips_extra_trigger(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="w", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    send_message(conn, root=root, room_id="t1", from_agent="claude", to_agent="codex", kind="message", status="final", summary="done", body="idempotency done")
    conn.close()
    calls = []
    monkeypatch.setattr(mailbox_eval, "_run_mailbox", lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "{}", ""))
    terminal, _ = mailbox_eval._poll_to_terminal(root, "t1", 1, {"extra_triggers": 1})
    assert terminal == "final"
    assert calls == []


def test_mailbox_eval_failed_real_run_stops_task(monkeypatch, tmp_path):
    calls = []

    def fake_start(mailbox_root, project, scenario, context_path):
        return "t1", {"claude_pane_id": 1, "codex_pane_id": 2, "relay_pane_id": 3}, Path("wezterm")

    def fake_run_mailbox(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")

    monkeypatch.setattr(mailbox_eval, "_start_real_task", fake_start)
    monkeypatch.setattr(mailbox_eval, "_poll_to_terminal", lambda *args, **kwargs: ("timeout", False))
    monkeypatch.setattr(mailbox_eval, "_capture_pane_snapshots", lambda *args, **kwargs: None)
    monkeypatch.setattr(mailbox_eval, "_run_mailbox", fake_run_mailbox)
    result = mailbox_eval.run_scenario(
        {
            "id": "AM-X",
            "name": "x",
            "goal": "g",
            "context": "c",
            "workspace_files": {"a.txt": "a"},
            "success": {"terminal_status": "final"},
        },
        keep=True,
        launch_real=True,
    )
    assert result.status == "fail"
    assert any(call[:2] == ("stop", "--root") and "--close-panes" in call for call in calls)
