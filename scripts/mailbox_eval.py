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

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX = SKILL_ROOT / "scripts" / "mailbox.py"
SCENARIO_DIR = SKILL_ROOT / "eval" / "scenarios"


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    status: str
    notes: list[str]
    root: str
    task_id: str | None = None


def _run_mailbox(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MAILBOX), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


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


def _poll_to_terminal(root: Path, task_id: str, timeout_s: int, scenario: dict[str, Any]) -> tuple[str, bool]:
    deadline = time.time() + timeout_s
    observed_blocked = False
    injected = False
    while time.time() < deadline:
        conn = connect_db(root)
        try:
            room = conn.execute("SELECT status, last_message_id FROM rooms WHERE id=?", (task_id,)).fetchone()
            status = room["status"]
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
            if status in {"final", "error", "stopped"}:
                return status, observed_blocked
        finally:
            conn.close()
        time.sleep(5)
    return "timeout", observed_blocked


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
    try:
        start_args = [
            "start" if launch_real else "init",
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
            start_args.extend(["--max-iters", "60"])
        rv = _run_mailbox(*start_args, timeout=120)
        if rv.returncode != 0:
            return ScenarioResult(scenario["id"], scenario["name"], "error", [rv.stderr or rv.stdout], str(work_root), task_id)
        task_id = json.loads(rv.stdout)["data"]["task_id"]
        if not launch_real:
            return ScenarioResult(scenario["id"], scenario["name"], "defined", ["scenario initialized only; pass --run-real to execute"], str(work_root), task_id)
        terminal, observed_blocked = _poll_to_terminal(mailbox_root, task_id, int(scenario.get("timeout_seconds", 420)), scenario)
        if terminal != scenario.get("success", {}).get("terminal_status", "final"):
            return ScenarioResult(scenario["id"], scenario["name"], "fail", [f"terminal status {terminal!r}"], str(work_root), task_id)
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
        return ScenarioResult(scenario["id"], scenario["name"], "pass" if not notes else "fail", notes, str(work_root), task_id)
    finally:
        if not keep and not launch_real:
            shutil.rmtree(work_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agent-mailbox behavioral scenarios")
    parser.add_argument("--scenario", help="Scenario id, e.g. AM-01")
    parser.add_argument("--run-real", action="store_true", help="Launch real Claude/Codex/WezTerm panes")
    parser.add_argument("--keep", action="store_true", help="Keep temp workspace after run")
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
