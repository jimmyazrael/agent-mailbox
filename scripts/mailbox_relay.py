from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from mailbox_common import (
    add_common_args,
    bounded,
    cdata_safe,
    file_lock,
    latest_message,
    load_state,
    parse_status_marker,
    peer_for,
    post_message,
    read_messages,
    relay_log_path,
    save_state,
    skill_root,
    utc_now,
)


BOOTSTRAP_PROMPT = """You are agent {agent} starting mailbox task {task_id}.

Goal: {goal}

Peer agent: {peer}
Project working directory: {project_cwd}

This is your first turn. State your understanding of the goal, propose an initial approach, and ask the peer for review or action. End with a MAILBOX_STATUS marker.
"""

TURN_PROMPT = """You are agent {agent} in mailbox task {task_id}. The peer ({peer}) has sent the following message. Read it, decide what to do for this turn, and respond.

<mailbox-peer-message author="{peer}" message_id="{message_id}" timestamp="{timestamp}">
<![CDATA[
{content}
]]>
</mailbox-peer-message>

Project working directory: {project_cwd}
Task goal: {goal}

Your turn now. End with a MAILBOX_STATUS marker.
"""


def _log(root: Path, task_id: str, line: str) -> None:
    path = relay_log_path(root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line.rstrip() + "\n")


def _build_prompt(root: Path, state: Dict[str, Any], agent: str) -> str:
    peer = peer_for(state, agent)
    project_cwd = state.get("project_cwd", "")
    msg = latest_message(root, state["task_id"])
    if msg is None:
        return BOOTSTRAP_PROMPT.format(
            agent=agent,
            peer=peer,
            task_id=state["task_id"],
            goal=state.get("goal", ""),
            project_cwd=project_cwd,
        )
    max_chars = int(state.get("limits", {}).get("max_peer_message_chars") or 10000)
    return TURN_PROMPT.format(
        agent=agent,
        peer=peer,
        task_id=state["task_id"],
        message_id=msg.get("message_id", ""),
        timestamp=msg.get("timestamp", ""),
        content=cdata_safe(bounded(msg.get("content", ""), max_chars)),
        project_cwd=project_cwd,
        goal=state.get("goal", ""),
    )


def _json_lines(text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _find_key(obj: Any, key: str) -> Optional[Any]:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _sum_usage(items: List[Dict[str, Any]], names: tuple[str, ...]) -> int:
    total = 0
    for item in items:
        for name in names:
            value = _find_key(item, name)
            if isinstance(value, int):
                total += value
                break
    return total


def _run_subprocess(cmd: List[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def _invoke_mock(args: argparse.Namespace, state: Dict[str, Any], agent: str, prompt: str, out_file: Path, prompt_file: Path) -> Dict[str, Any]:
    if not args.mock_responses:
        raise ValueError("--mock-responses is required for --backend mock")
    response_index = sum(1 for m in read_messages(args.root, args.task_id) if m.get("author") == agent)
    cmd = [
        sys.executable,
        str(skill_root() / "scripts" / "mock_agent.py"),
        "--agent",
        agent,
        "--response-index",
        str(response_index),
        "--responses-file",
        str(args.mock_responses),
        "--prompt-file",
        str(prompt_file),
        "--output-last-message",
        str(out_file),
    ]
    cp = _run_subprocess(cmd, Path(state.get("project_cwd") or Path.cwd()), int(state["limits"]["turn_timeout_seconds"]))
    if cp.returncode != 0:
        raise RuntimeError(f"mock agent failed: {cp.stderr.strip() or cp.stdout.strip()}")
    return {}


def _invoke_codex(args: argparse.Namespace, state: Dict[str, Any], agent: str, prompt: str, out_file: Path) -> Dict[str, Any]:
    project_cwd = Path(state.get("project_cwd") or Path.cwd())
    thread_id = state.get("sessions", {}).get("codex", {}).get("thread_id")
    if thread_id:
        cmd = [args.codex_cmd, "exec", "resume", thread_id, "--output-last-message", str(out_file), "--full-auto", prompt]
    else:
        cmd = [args.codex_cmd, "exec", "--json", "--output-last-message", str(out_file), "--full-auto", prompt]
    cp = _run_subprocess(cmd, project_cwd, int(state["limits"]["turn_timeout_seconds"]))
    if cp.returncode != 0:
        raise RuntimeError(f"codex failed: {cp.stderr.strip() or cp.stdout.strip()}")
    events = _json_lines(cp.stdout)
    updates: Dict[str, Any] = {"usage": {}}
    if not thread_id:
        parsed = _find_key(events, "thread_id")
        if not parsed:
            raise RuntimeError("codex did not emit thread_id")
        updates["sessions"] = {"codex": {"thread_id": parsed}}
    input_tokens = _sum_usage(events, ("input_tokens", "prompt_tokens"))
    output_tokens = _sum_usage(events, ("output_tokens", "completion_tokens"))
    if input_tokens:
        updates["usage"]["codex_input_tokens"] = int(state.get("usage", {}).get("codex_input_tokens", 0)) + input_tokens
    if output_tokens:
        updates["usage"]["codex_output_tokens"] = int(state.get("usage", {}).get("codex_output_tokens", 0)) + output_tokens
    if not updates["usage"]:
        updates.pop("usage")
    return updates


def _invoke_claude(args: argparse.Namespace, state: Dict[str, Any], agent: str, prompt: str, out_file: Path) -> Dict[str, Any]:
    project_cwd = Path(state.get("project_cwd") or Path.cwd())
    session_id = state.get("sessions", {}).get("claude", {}).get("session_id")
    first = False
    if not session_id:
        session_id = str(uuid.uuid4())
        first = True
    system_append = (skill_root() / "system-append.md").read_text(encoding="utf-8")
    if first:
        cmd = [args.claude_cmd, "-p", "--session-id", session_id, "--output-format", "json", "--append-system-prompt", system_append, "--permission-mode", "acceptEdits", prompt]
    else:
        cmd = [args.claude_cmd, "-p", "-r", session_id, "--output-format", "json", "--append-system-prompt", system_append, "--permission-mode", "acceptEdits", prompt]
    cp = _run_subprocess(cmd, project_cwd, int(state["limits"]["turn_timeout_seconds"]))
    if cp.returncode != 0:
        raise RuntimeError(f"claude failed: {cp.stderr.strip() or cp.stdout.strip()}")
    data = json.loads(cp.stdout)
    result = data.get("result")
    if not isinstance(result, str):
        raise RuntimeError("claude JSON did not include string result")
    out_file.write_text(result, encoding="utf-8", newline="\n")
    updates: Dict[str, Any] = {"sessions": {"claude": {"session_id": session_id}}}
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        updates["usage"] = {"known_cost_usd": float(state.get("usage", {}).get("known_cost_usd", 0.0)) + float(cost)}
    return updates


def _invoke_agent(args: argparse.Namespace, state: Dict[str, Any], agent: str, prompt: str, out_file: Path, prompt_file: Path) -> Dict[str, Any]:
    if args.backend == "mock":
        return _invoke_mock(args, state, agent, prompt, out_file, prompt_file)
    if agent == "codex":
        return _invoke_codex(args, state, agent, prompt, out_file)
    if agent == "claude":
        return _invoke_claude(args, state, agent, prompt, out_file)
    raise ValueError(f"unsupported real backend agent: {agent}")


def _set_running(root: Path, task_id: str, agent: str) -> Dict[str, Any]:
    with file_lock(root, task_id, owner=f"relay:{agent}:start"):
        state = load_state(root, task_id)
        state["status"] = "running"
        state["processing"] = {"agent": agent, "started_at": utc_now()}
        save_state(root, task_id, state)
        return state


def _check_limits(state: Dict[str, Any]) -> Optional[str]:
    limits = state.get("limits", {})
    usage = state.get("usage", {})
    if int(state.get("round", 0)) >= int(limits.get("max_rounds", 30)):
        return "max_rounds reached"
    max_cost = limits.get("max_total_cost_usd")
    if max_cost is not None and float(usage.get("known_cost_usd", 0.0)) >= float(max_cost):
        return "max known cost reached"
    max_tokens = limits.get("max_total_tokens")
    if max_tokens is not None:
        tokens = int(usage.get("codex_input_tokens", 0)) + int(usage.get("codex_output_tokens", 0))
        if tokens >= int(max_tokens):
            return "max Codex token cap reached"
    return None


def relay_once(args: argparse.Namespace) -> bool:
    state = load_state(args.root, args.task_id)
    if state.get("status") in {"final", "blocked", "error"}:
        return False
    limit_reason = _check_limits(state)
    if limit_reason:
        post_message(args.root, args.task_id, "relay", "user", "error", limit_reason, f"Relay stopped: {limit_reason}", next_turn=None)
        return False
    agent = state.get("turn")
    if not agent:
        post_message(args.root, args.task_id, "relay", "user", "error", "missing turn", "Relay stopped: state.turn is empty.", next_turn=None)
        return False
    state = _set_running(args.root, args.task_id, agent)
    peer = peer_for(state, agent)
    prompt = _build_prompt(args.root, state, agent)
    with tempfile.TemporaryDirectory(prefix="agent-mailbox-") as tmp:
        tmpdir = Path(tmp)
        prompt_file = tmpdir / "prompt.txt"
        out_file = tmpdir / "last-message.txt"
        prompt_file.write_text(prompt, encoding="utf-8", newline="\n")
        try:
            updates = _invoke_agent(args, state, agent, prompt, out_file, prompt_file)
            if not out_file.exists():
                raise RuntimeError(f"{agent} output file missing")
            output = out_file.read_text(encoding="utf-8")
            status, reason = parse_status_marker(output)
            summary = reason or f"{agent} turn complete"
            post_message(args.root, args.task_id, agent, peer, status, summary, output, next_turn=peer, state_updates=updates)
            _log(args.root, args.task_id, f"{agent}: {status} {summary}")
            return status == "continue"
        except subprocess.TimeoutExpired as exc:
            content = f"{agent} timed out after {exc.timeout} seconds."
            post_message(args.root, args.task_id, "relay", "user", "error", "subprocess timeout", content, next_turn=None)
            _log(args.root, args.task_id, content)
            return False
        except Exception as exc:
            content = f"{agent} turn failed: {type(exc).__name__}: {exc}"
            post_message(args.root, args.task_id, "relay", "user", "error", "agent turn failed", content, next_turn=None)
            _log(args.root, args.task_id, content)
            return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the agent mailbox relay")
    add_common_args(parser)
    parser.add_argument("--backend", choices=["mock", "real"], default="mock")
    parser.add_argument("--mock-responses", type=Path)
    parser.add_argument("--codex-cmd", default="codex")
    parser.add_argument("--claude-cmd", default="claude")
    parser.add_argument("--max-turns", type=int, default=None, help="Optional relay-process cap in addition to state limits")
    args = parser.parse_args()

    turns = 0
    while True:
        if args.max_turns is not None and turns >= args.max_turns:
            break
        should_continue = relay_once(args)
        turns += 1
        if not should_continue:
            break
    state = load_state(args.root, args.task_id)
    print(f"relay stopped: status={state.get('status')} round={state.get('round')} last_message_id={state.get('last_message_id')}")


if __name__ == "__main__":
    main()
