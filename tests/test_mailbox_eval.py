import json
import subprocess
import sys
from pathlib import Path

import mailbox_eval
from agent_chat import add_participant, connect_db, init_db, init_room, room_state_get, send_message, set_pane

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
        assert data["tier"] in {"real", "synthetic", "unit"}
        if data["tier"] == "synthetic":
            assert data.get("synthetic_action"), f"{path.name} is synthetic but has no synthetic_action"
        if data.get("synthetic_action"):
            assert data["tier"] == "synthetic", f"{path.name} has synthetic_action but tier is not synthetic"
        assert data["goal"]
        assert data["context"]
        assert data["workspace_files"]
        assert data["relay_version"] == "v2-outbox"
        assert data["success"]["terminal_status"] in {"final", "paused", "error"}
        if data["success"]["terminal_status"] == "error":
            assert data["success"].get("terminal_error_reason"), f"{path.name} has terminal_status=error but no terminal_error_reason"
        assert "codex_discovery_status" not in data["success"]
        if data["success"].get("codex_discovery_required"):
            assert data["success"]["codex_discovery_result"] in {"n/a", "pending", "discovered", "failed"}
        categories.add(data["category"])
    assert {"context-bootstrap", "blocked-resume", "idempotency", "recovery", "context-overload", "role-mode-protocol", "outbox-integrity", "outbox-safety-net", "safety-limit"}.issubset(categories)


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
    assert result["tier"] == "real"
    assert result["status"] == "defined"
    assert result["task_id"]


def test_mailbox_eval_runs_synthetic_outbox_integrity_scenario():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-13"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 0, rv.stderr
    result = json.loads(rv.stdout)
    assert result["scenario_id"] == "AM-13"
    assert result["tier"] == "synthetic"
    assert result["status"] == "pass"


def test_mailbox_eval_runs_synthetic_missing_outbox_scenario():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-14"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 0, rv.stderr
    result = json.loads(rv.stdout)
    assert result["scenario_id"] == "AM-14"
    assert result["tier"] == "synthetic"
    assert result["status"] == "pass"


def test_run_mailbox_accepts_env_overrides(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    mailbox_eval._run_mailbox(
        "status",
        env_overrides={"AGENT_MAILBOX_CLAUDE_PERMISSION_MODE": "bypassPermissions"},
    )
    assert seen["AGENT_MAILBOX_CLAUDE_PERMISSION_MODE"] == "bypassPermissions"


def test_mailbox_eval_extra_doorbell_targets_current_turn(monkeypatch, tmp_path):
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
    assert mailbox_eval._send_extra_doorbells(root, "t1", 2) is None
    assert calls == [
        ("doorbell", "--root", str(root), "--task-id", "t1", "--agent", "codex", "--format", "json"),
        ("doorbell", "--root", str(root), "--task-id", "t1", "--agent", "codex", "--format", "json"),
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


def test_mailbox_eval_terminal_room_skips_extra_doorbell(monkeypatch, tmp_path):
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
    terminal, _ = mailbox_eval._poll_to_terminal(root, "t1", 1, {"extra_doorbells": 1})
    assert terminal == "final"
    assert calls == []


def test_mailbox_eval_participation_notes_catch_single_agent_false_pass(tmp_path):
    root = tmp_path / "mb"
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="w", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    send_message(
        conn,
        root=root,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="outbox",
        status="blocked",
        summary="needs code",
        body="blocked",
    )
    send_message(
        conn,
        root=root,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="outbox",
        status="final",
        summary="done",
        body="R-2026-ALPHA",
    )

    notes = mailbox_eval._participation_notes(
        conn,
        "t1",
        {
            "terminal_status": "final",
            "required_outbox_authors": ["claude", "codex"],
            "min_outbox_messages_by_author": {"claude": 2, "codex": 1},
            "required_outbox_statuses_by_author": {"claude": ["blocked", "continue"], "codex": ["final"]},
            "final_from_agent": "codex",
        },
    )

    assert "missing outbox from required author: codex" in notes
    assert "outbox count for codex: 0, expected at least 1" in notes
    assert "missing continue outbox from claude" in notes
    assert "missing final outbox from codex" in notes
    assert "final message author: claude, expected codex" in notes


def test_mailbox_eval_owner_correlation_requires_declared_owner_to_write_next(tmp_path):
    root = tmp_path / "mb"
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="w", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    send_message(
        conn,
        root=root,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="outbox",
        status="continue",
        summary="handoff",
        body="Mode: EXECUTE\nOwner: codex\nNext action: write final\nDone when: final is written",
    )
    send_message(
        conn,
        root=root,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="outbox",
        status="final",
        summary="wrong owner",
        body="passive consensus",
    )

    notes = mailbox_eval._owner_correlation_notes(
        conn,
        root,
        "t1",
        {"owner_correlation": {"from_outbox_author": "claude", "next_outbox_author_must_match": True}},
    )

    assert notes == ["owner_correlation: declared owner 'codex', next outbox by 'claude'"]


def test_mailbox_eval_active_turn_injection_targets_current_turn(monkeypatch, tmp_path):
    root = tmp_path / "mb"
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="w", first_turn="claude")
    add_participant(conn, "t1", "claude")
    add_participant(conn, "t1", "codex")
    set_pane(conn, "t1", "codex", pane_id=12)
    send_message(
        conn,
        root=root,
        room_id="t1",
        from_agent="claude",
        to_agent="codex",
        kind="outbox",
        status="continue",
        summary="stale handoff",
        body="OLD-CODE-41",
    )
    conn.close()
    calls = []

    def fake_run_mailbox(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")

    monkeypatch.setattr(mailbox_eval, "_run_mailbox", fake_run_mailbox)
    terminal, _ = mailbox_eval._poll_to_terminal(
        root,
        "t1",
        1,
        {
            "inject_after_first_outbox": {
                "summary": "correction",
                "content": "NEW-CODE-88",
                "target": "next",
            }
        },
    )

    assert terminal == "timeout"
    assert calls[:1] == [
        (
            "inject",
            "--root",
            str(root),
            "--task-id",
            "t1",
            "--target",
            "next",
            "--summary",
            "correction",
            "--content",
            "NEW-CODE-88",
            "--format",
            "json",
        )
    ]


def test_am02_requires_codex_final_participation():
    data = json.loads((SCENARIOS / "02-blocked-resume.json").read_text(encoding="utf-8"))
    success = data["success"]
    assert success["final_from_agent"] == "codex"
    assert "codex" in success["required_outbox_authors"]
    assert success["min_outbox_messages_by_author"]["codex"] >= 1
    assert "final" in success["required_outbox_statuses_by_author"]["codex"]
    assert "Claude-only final is a failure" in data["context"]


def test_am05_requires_codex_final_participation():
    data = json.loads((SCENARIOS / "05-context-overload.json").read_text(encoding="utf-8"))
    success = data["success"]
    assert success["final_from_agent"] == "codex"
    assert "codex" in success["required_outbox_authors"]
    assert success["min_outbox_messages_by_author"]["codex"] >= 1
    assert "final" in success["required_outbox_statuses_by_author"]["codex"]
    assert "Claude must not write final" in data["context"]
    assert "Claude-only final is a scenario failure" in data["context"] or "Claude final is a scenario failure" in data["context"]


def test_am07_requires_single_continue_per_agent_and_claude_final():
    data = json.loads((SCENARIOS / "07-mid-handoff-duplicate-trigger.json").read_text(encoding="utf-8"))
    success = data["success"]
    assert success["final_from_agent"] == "claude"
    assert success["max_messages"] == 4
    assert success["required_outbox_statuses_by_author"]["claude"] == ["continue", "final"]
    assert success["required_outbox_statuses_by_author"]["codex"] == ["continue"]
    assert "Claude writes exactly one continue" in data["context"]
    assert "Codex must write exactly one continue confirmation" in data["context"]
    assert "Claude must write the only final" in data["context"]
    assert "Additional ping-pong continue messages are scenario failures" in data["context"] or "Additional continue messages are scenario failures" in data["context"]


def test_real_conversation_scenarios_require_participation_contracts():
    audited = ["AM-03", "AM-04", "AM-05", "AM-06", "AM-07", "AM-09", "AM-11", "AM-12"]
    by_id = {json.loads(path.read_text(encoding="utf-8"))["id"]: json.loads(path.read_text(encoding="utf-8")) for path in SCENARIOS.glob("*.json")}
    for scenario_id in audited:
        success = by_id[scenario_id]["success"]
        assert set(success["required_outbox_authors"]) == {"claude", "codex"}
        assert set(success["min_outbox_messages_by_author"]) == {"claude", "codex"}
        assert set(success["required_outbox_statuses_by_author"]) == {"claude", "codex"}
        assert success["final_from_agent"] in {"claude", "codex"}
        assert "final" in success["required_outbox_statuses_by_author"][success["final_from_agent"]]


def test_am04_rediscovery_requires_codex_outbox_after_rediscovery():
    data = json.loads((SCENARIOS / "04-codex-rediscovery.json").read_text(encoding="utf-8"))
    success = data["success"]
    assert success["codex_discovery_required"] is True
    assert success["codex_discovery_result"] == "discovered"
    assert success["min_outbox_messages_by_author"]["codex"] >= 1
    assert "continue" in success["required_outbox_statuses_by_author"]["codex"]
    assert success["final_from_agent"] == "claude"
    assert "after rediscovery runs" in data["context"]


def test_launch_relay_pane_uses_split_when_geometry_fits(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "33\n", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    pane_id, method = mailbox_eval._launch_relay_pane(
        wezterm_exe=Path("wezterm"),
        workspace="agent-mailbox-t1",
        codex_pane_id=12,
        project=tmp_path,
        relay_cmd=["relay"],
    )
    assert pane_id == 33
    assert method == "split"
    assert len(calls) == 1
    assert "split-pane" in calls[0]


def test_launch_relay_pane_falls_back_to_spawn_on_no_space_for_split(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "split-pane" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "Error: No space for split!")
        return subprocess.CompletedProcess(argv, 0, "44\n", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    pane_id, method = mailbox_eval._launch_relay_pane(
        wezterm_exe=Path("wezterm"),
        workspace="agent-mailbox-t1",
        codex_pane_id=12,
        project=tmp_path,
        relay_cmd=["relay"],
    )
    assert pane_id == 44
    assert method == "spawn"
    assert "split-pane" in calls[0]
    assert calls[1][1:4] == ["cli", "--prefer-mux", "spawn"]
    assert "--workspace" in calls[1]
    assert "agent-mailbox-t1" in calls[1]


def test_launch_relay_pane_propagates_other_split_errors(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "auth error")

    monkeypatch.setattr("subprocess.run", fake_run)
    try:
        mailbox_eval._launch_relay_pane(
            wezterm_exe=Path("wezterm"),
            workspace="agent-mailbox-t1",
            codex_pane_id=12,
            project=tmp_path,
            relay_cmd=["relay"],
        )
    except RuntimeError as exc:
        assert "auth error" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert len(calls) == 1
    assert "spawn" not in calls[0]


def test_launch_relay_pane_propagates_other_spawn_errors(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "split-pane" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "No space for split!")
        return subprocess.CompletedProcess(argv, 1, "", "mux error")

    monkeypatch.setattr("subprocess.run", fake_run)
    try:
        mailbox_eval._launch_relay_pane(
            wezterm_exe=Path("wezterm"),
            workspace="agent-mailbox-t1",
            codex_pane_id=12,
            project=tmp_path,
            relay_cmd=["relay"],
        )
    except RuntimeError as exc:
        assert "mux error" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert "split-pane" in calls[0]
    assert "spawn" in calls[1]


def test_start_real_task_records_relay_launch_method(monkeypatch, tmp_path):
    calls = []

    def fake_run_mailbox(*args, **kwargs):
        calls.append(args)
        if args[0] == "init":
            root = Path(args[args.index("--root") + 1])
            init_db(root)
            conn = connect_db(root)
            init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path / "project", workspace="agent-mailbox-t1", first_turn="claude")
            add_participant(conn, "t1", "claude")
            add_participant(conn, "t1", "codex")
            conn.close()
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"task_id": "t1"}}), "")
        if args[0] == "launch-tui":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"data": {"workspace": "agent-mailbox-t1", "claude_pane_id": 1, "codex_pane_id": 2}}),
                "",
            )
        return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")

    monkeypatch.setattr(mailbox_eval, "_run_mailbox", fake_run_mailbox)
    monkeypatch.setattr(mailbox_eval, "_accept_trust_prompt", lambda *args, **kwargs: False)
    monkeypatch.setattr("tui_launcher.find_wezterm", lambda: Path("wezterm"))
    monkeypatch.setattr(mailbox_eval, "_launch_relay_pane", lambda **kwargs: (55, "spawn"))

    task_id, launch, wezterm_exe = mailbox_eval._start_real_task(
        tmp_path / "mb",
        tmp_path / "project",
        {"id": "AM-X", "name": "x", "goal": "g", "timeout_seconds": 10},
        tmp_path / "context.md",
    )
    assert task_id == "t1"
    assert wezterm_exe == Path("wezterm")
    assert launch["relay_pane_id"] == 55
    assert launch["relay_launch_method"] == "spawn"
    assert any(call[0] == "repair" and "--rebind-pane" in call for call in calls)
    conn = connect_db(tmp_path / "mb")
    try:
        assert room_state_get(conn, "t1", "relay_launch") == {"method": "spawn", "pane_id": 55}
    finally:
        conn.close()


def test_mailbox_eval_reports_paused_relay_reason(tmp_path):
    root = tmp_path / "mb"
    init_db(root)
    conn = connect_db(root)
    init_room(conn, room_id="t1", name="T1", purpose="p", project_cwd=tmp_path, workspace="w", first_turn="claude")
    conn.execute("UPDATE tui_relay_state SET paused=1, pause_reason='malformed_outbox:invalid_status' WHERE room_id='t1'")
    conn.close()
    terminal, _ = mailbox_eval._poll_to_terminal(root, "t1", 30, {})
    assert terminal == "paused:malformed_outbox:invalid_status"


def test_mailbox_eval_failed_real_run_stops_task(monkeypatch, tmp_path):
    calls = []

    def fake_start(mailbox_root, project, scenario, context_path):
        return "t1", {"claude_pane_id": 1, "codex_pane_id": 2, "relay_pane_id": 3}, Path("wezterm")

    def fake_run_mailbox(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")

    monkeypatch.setattr(mailbox_eval, "_start_real_task", fake_start)
    monkeypatch.setattr(mailbox_eval, "_poll_to_terminal", lambda *args, **kwargs: ("timeout", False))
    monkeypatch.setattr(mailbox_eval, "_capture_pane_snapshots", lambda *args, **kwargs: {"1": "pane-1.txt"})
    monkeypatch.setattr(mailbox_eval, "_run_mailbox", fake_run_mailbox)
    result = mailbox_eval.run_scenario(
            {
                "id": "AM-X",
                "name": "x",
                "relay_version": "v2-outbox",
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


def test_mailbox_eval_participation_validation_uses_open_connection(monkeypatch, tmp_path):
    import mailbox_eval

    root = tmp_path / "mb"
    project = tmp_path / "project"
    task_id = "t1"

    def fake_start(mailbox_root, project_path, scenario, context_path):
        init_db(mailbox_root)
        conn = connect_db(mailbox_root)
        from agent_chat import init_room

        init_room(conn, room_id=task_id, name="T1", purpose="p", project_cwd=project_path, workspace="w", first_turn="claude")
        add_participant(conn, task_id, "claude")
        add_participant(conn, task_id, "codex")
        send_message(conn, root=mailbox_root, room_id=task_id, from_agent="claude", to_agent="codex", kind="outbox", status="continue", summary="handoff", body="R-2026-ALPHA")
        send_message(conn, root=mailbox_root, room_id=task_id, from_agent="codex", to_agent="claude", kind="outbox", status="final", summary="done", body="R-2026-ALPHA")
        conn.close()
        return task_id, {"claude_pane_id": 1, "codex_pane_id": 2, "relay_pane_id": 3}, Path("wezterm")

    monkeypatch.setattr(mailbox_eval, "_start_real_task", fake_start)
    monkeypatch.setattr(mailbox_eval, "_poll_to_terminal", lambda *args, **kwargs: ("final", False))
    monkeypatch.setattr(mailbox_eval, "_capture_pane_snapshots", lambda *args, **kwargs: {"1": "pane-1.txt"})
    result = mailbox_eval.run_scenario(
        {
            "id": "AM-X",
            "name": "x",
            "relay_version": "v2-outbox",
            "goal": "g",
            "context": "c",
            "workspace_files": {"a.txt": "a"},
            "success": {
                "terminal_status": "final",
                "required_transcript_terms": ["R-2026-ALPHA"],
                "required_outbox_authors": ["claude", "codex"],
                "final_from_agent": "codex",
            },
        },
        keep=True,
        launch_real=True,
    )
    assert result.status == "pass"
    assert result.artifacts == {"pane_snapshots": {"1": "pane-1.txt"}}


def test_mailbox_eval_am12_exercises_cleanup_runtime():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-12"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 0, rv.stderr
    result = json.loads(rv.stdout)
    assert result["scenario_id"] == "AM-12"
    assert result["tier"] == "synthetic"
    assert result["status"] == "pass"


def test_stop_real_task_reports_post_stop_mux_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mailbox_eval,
        "_run_mailbox",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"ok": true}', ""),
    )

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr("subprocess.run", fake_run)
    notes = mailbox_eval._stop_real_task(tmp_path / "mb", "t1", wezterm_exe=Path("wezterm"))
    assert notes == ["post-stop mux health check timed out"]
