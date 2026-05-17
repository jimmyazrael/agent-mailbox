#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_chat import connect_db, export_transcript_md
from pane_control import build_activate_pane_argv, build_get_text_argv, build_send_text_argv, build_split_argv

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX = SKILL_ROOT / "scripts" / "mailbox.py"
SCENARIO_DIR = SKILL_ROOT / "eval" / "scenarios"
ACTION_REDISCOVER_CODEX = "rediscover_codex_after_first_codex_turn"


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    status: str
    notes: list[str]
    root: str
    task_id: str | None = None


def _run_mailbox(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    # Real TUI launches can leave descendant processes holding inherited pipe
    # handles. Capturing through temp files waits only for the direct child.
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stdout, tempfile.TemporaryFile(
        "w+", encoding="utf-8", errors="replace"
    ) as stderr:
        rv = subprocess.run(
            [sys.executable, str(MAILBOX), *args],
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(rv.args, rv.returncode, stdout.read(), stderr.read())


def _get_pane_text(wezterm_exe: Path, pane_id: int) -> str:
    rv = subprocess.run(
        build_get_text_argv(wezterm_exe=wezterm_exe, pane_id=pane_id, start_line=-80),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    return rv.stdout or ""


def _pane_tail(text: str, lines: int = 12) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _send_choice(wezterm_exe: Path, pane_id: int, text: str) -> None:
    subprocess.run(
        build_activate_pane_argv(wezterm_exe=wezterm_exe, pane_id=pane_id),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    time.sleep(0.25)
    subprocess.run(
        build_send_text_argv(wezterm_exe=wezterm_exe, pane_id=pane_id, text=text, no_paste=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def _accept_trust_prompt(wezterm_exe: Path, pane_id: int, *, timeout_s: int = 12, no_prompt_grace_s: int = 3) -> bool:
    deadline = time.time() + timeout_s
    no_prompt_deadline = time.time() + no_prompt_grace_s
    while time.time() < deadline:
        text = _get_pane_text(wezterm_exe, pane_id).lower()
        if "do you trust" in text or "yes, i trust" in text or "yes, continue" in text:
            for _ in range(2):
                _send_choice(wezterm_exe, pane_id, "1\r")
                time.sleep(3)
                text = _get_pane_text(wezterm_exe, pane_id).lower()
                if "do you trust" not in text and "yes, i trust" not in text and "yes, continue" not in text:
                    break
            return True
        if text.strip() and time.time() >= no_prompt_deadline:
            return False
        time.sleep(1)
    return False


def _approve_if_prompted(wezterm_exe: Path, pane_id: int) -> bool:
    text = _pane_tail(_get_pane_text(wezterm_exe, pane_id)).lower()
    if "update available" in text and "skip until next version" in text:
        _send_choice(wezterm_exe, pane_id, "2\r")
        return True
    if "this command requires approval" not in text and "requires approval" not in text and "do you want to proceed" not in text:
        return False
    _send_choice(wezterm_exe, pane_id, "1\r")
    return True


def _capture_pane_snapshots(wezterm_exe: Path, pane_ids: list[int], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for pane_id in pane_ids:
        try:
            text = _get_pane_text(wezterm_exe, pane_id)
        except Exception as exc:
            text = f"<failed to capture pane {pane_id}: {exc}>"
        (out_dir / f"pane-{pane_id}.txt").write_text(text, encoding="utf-8", newline="\n")


def _write_workspace(project: Path, files: dict[str, str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel, text in files.items():
        path = (project / rel).resolve()
        path.relative_to(project.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        hashes[rel] = str(path.stat().st_mtime_ns) + ":" + str(path.stat().st_size)
    return hashes


def _file_fingerprint(project: Path, rel: str) -> str | None:
    path = project / rel
    if not path.exists():
        return None
    return str(path.stat().st_mtime_ns) + ":" + str(path.stat().st_size)


def _load_scenarios() -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(SCENARIO_DIR.glob("*.json"))]


def _current_turn(root: Path, task_id: str) -> str | None:
    conn = connect_db(root)
    try:
        row = conn.execute("SELECT turn FROM rooms WHERE id=?", (task_id,)).fetchone()
        return row["turn"] if row else None
    finally:
        conn.close()


def _last_message_from(root: Path, task_id: str, agent: str) -> int:
    conn = connect_db(root)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room_id=? AND from_agent=?",
            (task_id, agent),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _send_extra_triggers(root: Path, task_id: str, count: int) -> str | None:
    for _ in range(max(0, count)):
        turn = _current_turn(root, task_id)
        if not turn:
            return "extra trigger skipped: no current turn"
        rv = _run_mailbox(
            "trigger",
            "--root",
            str(root),
            "--task-id",
            task_id,
            "--agent",
            turn,
            "--format",
            "json",
        )
        if rv.returncode != 0:
            return f"extra trigger failed for {turn}: {rv.stderr or rv.stdout}"
    return None


def _run_action(root: Path, task_id: str, action: str) -> str | None:
    if action != ACTION_REDISCOVER_CODEX:
        return f"unknown action: {action}"
    rv = _run_mailbox(
        "repair",
        "--root",
        str(root),
        "--task-id",
        task_id,
        "--rediscover-codex",
        "--format",
        "json",
    )
    if rv.returncode != 0:
        return f"action {action} failed: {rv.stderr or rv.stdout}"
    return None


def _poll_to_terminal(
    root: Path,
    task_id: str,
    timeout_s: int,
    scenario: dict[str, Any],
    *,
    wezterm_exe: Path | None = None,
    pane_ids: list[int] | None = None,
) -> tuple[str, bool]:
    deadline = time.time() + timeout_s
    observed_blocked = False
    injected = False
    extra_triggers_sent = False
    actions_done: set[str] = set()
    first_claude_seen = _last_message_from(root, task_id, "claude")
    first_codex_seen = _last_message_from(root, task_id, "codex")
    while time.time() < deadline:
        conn = connect_db(root)
        try:
            room = conn.execute("SELECT status, last_message_id FROM rooms WHERE id=?", (task_id,)).fetchone()
            status = room["status"]
            if status in {"final", "error", "stopped"}:
                return status, observed_blocked
            if status == "blocked":
                observed_blocked = True
                injection = scenario.get("inject_on_blocked")
                if injection and not injected:
                    injected = True
                    rv = _run_mailbox(
                        "inject",
                        "--root",
                        str(root),
                        "--task-id",
                        task_id,
                        "--summary",
                        injection.get("summary", "eval injection"),
                        "--content",
                        injection.get("content", ""),
                        "--format",
                        "json",
                    )
                    if rv.returncode != 0:
                        return "error", observed_blocked
            if not extra_triggers_sent and int(scenario.get("extra_triggers", 0)) > 0:
                if _last_message_from(root, task_id, "claude") > first_claude_seen or _last_message_from(root, task_id, "codex") > first_codex_seen:
                    extra_triggers_sent = True
                    if _send_extra_triggers(root, task_id, int(scenario["extra_triggers"])) is not None:
                        return "error", observed_blocked
            if ACTION_REDISCOVER_CODEX in scenario.get("actions", []) and ACTION_REDISCOVER_CODEX not in actions_done:
                if _last_message_from(root, task_id, "codex") > first_codex_seen:
                    actions_done.add(ACTION_REDISCOVER_CODEX)
                    if _run_action(root, task_id, ACTION_REDISCOVER_CODEX) is not None:
                        return "error", observed_blocked
        finally:
            conn.close()
        if wezterm_exe is not None and pane_ids:
            for pane_id in pane_ids:
                _approve_if_prompted(wezterm_exe, pane_id)
        time.sleep(5)
    return "timeout", observed_blocked


def _start_real_task(mailbox_root: Path, project: Path, scenario: dict[str, Any], context_path: Path) -> tuple[str, dict[str, Any], Path]:
    init_args = [
        "init",
        "--root",
        str(mailbox_root),
        "--prefix",
        scenario["id"].lower(),
        "--label",
        scenario["name"],
        "--goal",
        scenario["goal"],
        "--project-cwd",
        str(project),
        "--first-turn",
        scenario.get("first_turn", "claude"),
        "--context-file",
        str(context_path),
        "--format",
        "json",
    ]
    rv = _run_mailbox(*init_args, timeout=120)
    if rv.returncode != 0:
        raise RuntimeError(rv.stderr or rv.stdout)
    task_id = json.loads(rv.stdout)["data"]["task_id"]

    launch_rv = _run_mailbox(
        "launch-tui",
        "--root",
        str(mailbox_root),
        "--task-id",
        task_id,
        "--no-chat",
        "--no-control-panel",
        "--format",
        "json",
        timeout=120,
    )
    if launch_rv.returncode != 0:
        raise RuntimeError(launch_rv.stderr or launch_rv.stdout)
    launch = json.loads(launch_rv.stdout)["data"]

    from tui_launcher import find_wezterm

    wezterm_exe = find_wezterm()
    for pane_key in ("claude_pane_id", "codex_pane_id"):
        # Fresh temp workspaces usually prompt for trust. Already-trusted
        # environments do not, so this is best-effort rather than required.
        _accept_trust_prompt(wezterm_exe, int(launch[pane_key]))

    # The relay poll interval defaults to 2s. Keep it alive for at least the
    # scenario timeout plus margin; otherwise real agents can be mid-turn when
    # the relay exits and the room will stall in "waiting".
    relay_max_iters = max(60, int(scenario.get("timeout_seconds", 420)) // 2 + 60)
    relay_cmd = [
        "cmd",
        "/c",
        str(SKILL_ROOT / "scripts" / "launch_relay_pane.cmd"),
        task_id,
        str(mailbox_root),
        str(project),
        str(MAILBOX),
        str(relay_max_iters),
    ]
    relay_rv = subprocess.run(
        build_split_argv(
            wezterm_exe=wezterm_exe,
            source_pane_id=int(launch["codex_pane_id"]),
            direction="bottom",
            percent=25,
            cwd=project,
            cmd=relay_cmd,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if relay_rv.returncode != 0:
        raise RuntimeError(relay_rv.stderr or relay_rv.stdout)
    relay_pane_id = None
    if relay_rv.stdout.strip().isdigit():
        relay_pane_id = int(relay_rv.stdout.strip())
        _run_mailbox(
            "repair",
            "--root",
            str(mailbox_root),
            "--task-id",
            task_id,
            "--rebind-pane",
            "--agent",
            "relay",
            "--pane-id",
            str(relay_pane_id),
        )
    launch["relay_pane_id"] = relay_pane_id
    return task_id, launch, wezterm_exe


def _stop_real_task(mailbox_root: Path, task_id: str) -> None:
    try:
        _run_mailbox(
            "stop",
            "--root",
            str(mailbox_root),
            "--task-id",
            task_id,
            "--close-panes",
            "--yes",
            "--format",
            "json",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        # The scenario result is more important than a stuck cleanup command.
        # External cleanup can still reap panes by workspace/process name.
        pass


def run_scenario(scenario: dict[str, Any], *, keep: bool = False, launch_real: bool = False) -> ScenarioResult:
    work_root = Path(tempfile.mkdtemp(prefix=f"agent_mailbox_eval_{scenario['id']}_"))
    project = work_root / "project"
    mailbox_root = work_root / "mailbox"
    project.mkdir(parents=True, exist_ok=True)
    fingerprints = _write_workspace(project, scenario.get("workspace_files", {}))
    context_path = work_root / "context.md"
    context_path.write_text(scenario.get("context", ""), encoding="utf-8", newline="\n")
    notes: list[str] = []
    task_id: str | None = None
    result: ScenarioResult | None = None
    try:
        start_args = [
            "init",
            "--root",
            str(mailbox_root),
            "--prefix",
            scenario["id"].lower(),
            "--label",
            scenario["name"],
            "--goal",
            scenario["goal"],
            "--project-cwd",
            str(project),
            "--first-turn",
            scenario.get("first_turn", "claude"),
            "--context-file",
            str(context_path),
            "--format",
            "json",
        ]
        if launch_real:
            try:
                task_id, launch, wezterm_exe = _start_real_task(mailbox_root, project, scenario, context_path)
            except Exception as exc:
                result = ScenarioResult(scenario["id"], scenario["name"], "error", [str(exc)], str(work_root), task_id)
                return result
        else:
            rv = _run_mailbox(*start_args, timeout=120)
            if rv.returncode != 0:
                result = ScenarioResult(scenario["id"], scenario["name"], "error", [rv.stderr or rv.stdout], str(work_root), task_id)
                return result
            task_id = json.loads(rv.stdout)["data"]["task_id"]
        if not launch_real:
            result = ScenarioResult(scenario["id"], scenario["name"], "defined", ["scenario initialized only; pass --run-real to execute"], str(work_root), task_id)
            return result
        terminal, observed_blocked = _poll_to_terminal(
            mailbox_root,
            task_id,
            int(scenario.get("timeout_seconds", 420)),
            scenario,
            wezterm_exe=wezterm_exe,
            pane_ids=[int(launch["claude_pane_id"]), int(launch["codex_pane_id"])],
        )
        _capture_pane_snapshots(
            wezterm_exe,
            [int(x) for x in (launch.get("claude_pane_id"), launch.get("codex_pane_id"), launch.get("relay_pane_id")) if x is not None],
            work_root / "pane-snapshots",
        )
        if terminal != scenario.get("success", {}).get("terminal_status", "final"):
            result = ScenarioResult(scenario["id"], scenario["name"], "fail", [f"terminal status {terminal!r}"], str(work_root), task_id)
            return result
        conn = connect_db(mailbox_root)
        try:
            transcript = export_transcript_md(conn, root=mailbox_root, room_id=task_id)
            message_count = conn.execute("SELECT COUNT(*) FROM messages WHERE room_id=?", (task_id,)).fetchone()[0]
            sessions = {
                row["agent"]: dict(row)
                for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
            }
        finally:
            conn.close()
        success = scenario.get("success", {})
        for term in success.get("required_transcript_terms", []):
            if term not in transcript:
                notes.append(f"missing transcript term: {term}")
        if success.get("must_observe_status") == "blocked" and not observed_blocked:
            notes.append("blocked status was not observed")
        if "max_messages" in success and message_count > int(success["max_messages"]):
            notes.append(f"too many messages: {message_count}")
        if success.get("codex_discovery_status") and sessions.get("codex", {}).get("discovery_status") != success["codex_discovery_status"]:
            notes.append(f"codex discovery status: {sessions.get('codex', {}).get('discovery_status')}")
        for rel in success.get("forbidden_file_changes", []):
            if _file_fingerprint(project, rel) != fingerprints.get(rel):
                notes.append(f"forbidden file changed: {rel}")
        result = ScenarioResult(scenario["id"], scenario["name"], "pass" if not notes else "fail", notes, str(work_root), task_id)
        return result
    finally:
        if launch_real and task_id and (not keep or (result is not None and result.status != "pass")):
            try:
                _stop_real_task(mailbox_root, task_id)
            except Exception:
                pass
        if not keep and not launch_real:
            shutil.rmtree(work_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agent-mailbox behavioral scenarios")
    parser.add_argument("--scenario", help="Scenario id, e.g. AM-01")
    parser.add_argument("--run-real", action="store_true", help="Launch real Claude/Codex/WezTerm panes")
    parser.add_argument("--keep", action="store_true", help="Keep temp workspace after run. Failed real runs still stop panes.")
    args = parser.parse_args()
    scenarios = _load_scenarios()
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
    if not scenarios:
        print("No scenarios matched", file=sys.stderr)
        return 2
    if args.run_real and os.environ.get("AGENT_MAILBOX_RUN_REAL_SMOKE") != "1":
        print("Refusing real run: set AGENT_MAILBOX_RUN_REAL_SMOKE=1", file=sys.stderr)
        return 2
    results = [run_scenario(s, keep=args.keep, launch_real=args.run_real) for s in scenarios]
    for result in results:
        print(json.dumps(result.__dict__, ensure_ascii=False))
    return 0 if all(r.status in {"pass", "defined"} for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
