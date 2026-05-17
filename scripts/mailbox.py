#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from agent_chat import (
    DDL,
    ack_message,
    add_participant,
    connect_db,
    ensure_outbox_dirs,
    export_transcript_md,
    get_message_body,
    init_db,
    init_room,
    list_rooms,
    peek_latest,
    read_unread,
    resolve_task_id,
    room_state_get,
    room_state_set,
    send_message,
    set_pane,
    set_session_metadata,
    update_codex_discovery,
)
from mailbox_lib import default_root, generate_task_id, peer_for, pid_exists, utc_now

TERMINAL_ROOM_STATUSES = {"final", "error", "stopped"}

PROTOCOL_HEADER_FIELDS = ("Mode", "Coordinator", "Owner", "Reviewer", "Next action", "Done when")
VALID_PROTOCOL_MODES = {
    "DISCUSS",
    "EXECUTE",
    "REVIEW",
    "BLOCKED",
    "DONE",
    "INVESTIGATE",
    "RESEARCH",
    "PLAN",
    "COORDINATE",
    "INFORM",
}


def _header_value(body: str, field: str) -> str | None:
    match = re.search(rf"(?im)^[ \t]*{re.escape(field)}[ \t]*:[ \t]*([^\r\n]*)[ \t]*$", body or "")
    return match.group(1).strip() if match else None


def lint_protocol_header(*, status: str, body: str, participants: Optional[set[str]] = None) -> list[str]:
    if status in {"final", "error"}:
        return []
    warnings: list[str] = []
    mode = _header_value(body, "Mode")
    if not mode:
        warnings.append("missing protocol header: Mode")
    elif mode.strip().upper() not in VALID_PROTOCOL_MODES:
        warnings.append(f"unknown protocol Mode: {mode}")
    for field in PROTOCOL_HEADER_FIELDS[1:]:
        if not _header_value(body, field):
            warnings.append(f"missing protocol header: {field}")
    if status == "blocked" and not _header_value(body, "Blocked on"):
        warnings.append("blocked posts should include protocol header: Blocked on")
    next_action = (_header_value(body, "Next action") or "").strip().lower()
    blocked_on = (_header_value(body, "Blocked on") or "").strip().lower()
    if status == "continue" and next_action in {"", "none", "n/a", "na"} and blocked_on in {"", "none", "n/a", "na"}:
        warnings.append("non-terminal posts must name a concrete Next action or Blocked on")
    owner = (_header_value(body, "Owner") or "").strip().lower()
    mode_norm = (mode or "").strip().upper()
    if (
        participants is not None
        and status == "continue"
        and mode_norm in {"EXECUTE", "REVIEW"}
        and owner not in {"", "none", "n/a", "na", "-"}
        and owner not in participants
    ):
        warnings.append(f"protocol Owner is not a participant: {owner}")
    return warnings


def _root(args) -> Path:
    return (args.root or default_root()).expanduser()


def _emit(args, *, ok: bool, data: Optional[dict[str, Any]] = None, error: Optional[str] = None) -> int:
    if getattr(args, "format", "human") == "json":
        print(json.dumps({"ok": ok, "data": data or {}, "error": error}, default=str))
    elif ok:
        for key, value in (data or {}).items():
            print(f"{key}: {value}")
    else:
        print(f"ERROR: {error}", file=sys.stderr)
    return 0 if ok else 2


def _debug_timing(message: str) -> None:
    if os.environ.get("AGENT_MAILBOX_DEBUG_TIMING") == "1":
        print(f"[agent-mailbox timing] {message}", file=sys.stderr, flush=True)


def _common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--root", type=Path)
    subparser.add_argument("--format", choices=["human", "json"], default="human")


def _chat_flags(subparser: argparse.ArgumentParser) -> None:
    group = subparser.add_mutually_exclusive_group()
    group.add_argument("--with-chat", dest="with_chat", action="store_true", help="open the read-only chat transcript view (default)")
    group.add_argument("--no-chat", dest="with_chat", action="store_false", help="do not open the chat transcript view")
    subparser.set_defaults(with_chat=True)
    subparser.add_argument("--chat-poll-interval-s", type=float, default=1.5)


def _control_flags(subparser: argparse.ArgumentParser) -> None:
    group = subparser.add_mutually_exclusive_group()
    group.add_argument("--with-control-panel", dest="with_control_panel", action="store_true", help="open the control panel view (default)")
    group.add_argument("--no-control-panel", dest="with_control_panel", action="store_false", help="do not open the control panel view")
    subparser.set_defaults(with_control_panel=True)


def _resolve(conn, task_id: str) -> str:
    return resolve_task_id(conn, task_id)


def _startup_not_ready_reason(startup: dict[str, Any]) -> str:
    agents = startup.get("agents") or {}
    if agents:
        return "startup_not_ready:" + ",".join(
            f"{agent}={info.get('state', 'unknown')}" for agent, info in sorted(agents.items())
        )
    return "startup_not_ready:workspace=missing"


def _validate_task_startup(conn: sqlite3.Connection, *, task_id: str, wezterm_exe: Path) -> dict[str, Any]:
    from tui_launcher import validate_workspace_startup

    room = conn.execute("SELECT workspace FROM rooms WHERE id=?", (task_id,)).fetchone()
    panes = {
        row["pane_role"]: row["pane_id"]
        for row in conn.execute("SELECT pane_role, pane_id FROM panes WHERE room_id=?", (task_id,)).fetchall()
    }
    missing = [agent for agent in ("claude", "codex") if panes.get(agent) is None]
    if room is None or missing:
        return {
            "workspace": room["workspace"] if room else None,
            "visible": False,
            "pane_count": 0,
            "agents": {agent: {"pane_id": panes.get(agent), "state": "missing_pane"} for agent in ("claude", "codex")},
            "ready": False,
        }
    return validate_workspace_startup(
        wezterm_exe=wezterm_exe,
        workspace=room["workspace"],
        claude_pane_id=int(panes["claude"]),
        codex_pane_id=int(panes["codex"]),
    )


def _apply_relay_startup_gate(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    startup: dict[str, Any],
    clear_if_ready: bool,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if startup.get("ready"):
            if clear_if_ready:
                conn.execute(
                    "UPDATE tui_relay_state SET paused=0, pause_reason=NULL WHERE room_id=?",
                    (task_id,),
                )
        else:
            conn.execute(
                "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
                (_startup_not_ready_reason(startup), task_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _spawn_workspace_tab(
    *,
    wezterm_exe: Path,
    workspace: str,
    cwd: Path,
    cmd: list[str],
) -> int | None:
    from pane_control import build_list_argv, build_spawn_argv
    from tui_launcher import lookup_pane, parse_wezterm_list

    list_rv = subprocess.run(
        build_list_argv(wezterm_exe=wezterm_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    list_rv.check_returncode()
    workspace_panes = lookup_pane(parse_wezterm_list(list_rv.stdout), workspace=workspace)
    if not workspace_panes:
        raise RuntimeError(f"workspace not found for tab spawn: {workspace}")
    window_id = int(workspace_panes[0]["window_id"])
    rv = subprocess.run(
        build_spawn_argv(
            wezterm_exe=wezterm_exe,
            workspace=workspace,
            cwd=cwd,
            cmd=cmd,
            new_window=False,
            window_id=window_id,
        ),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    rv.check_returncode()
    text = rv.stdout.strip()
    return int(text) if text.isdigit() else None


def _init_task(args, root: Path) -> dict[str, Any]:
    init_db(root)
    task_id = args.task_id or generate_task_id(args.prefix, args.label or "")
    workspace = f"agent-mailbox-{task_id}"
    claude_uuid = str(uuid.uuid4())
    conn = connect_db(root)
    try:
        init_room(
            conn,
            room_id=task_id,
            name=args.label or task_id,
            purpose=args.goal,
            project_cwd=Path(args.project_cwd).resolve(),
            workspace=workspace,
            first_turn=args.first_turn,
            parent_room_id=args.parent_task_id,
            exit_condition=args.exit_condition,
        )
        ensure_outbox_dirs(root, task_id)
        for agent in ("claude", "codex"):
            add_participant(conn, task_id, agent, role="worker" if agent == args.first_turn else "reviewer")
        set_session_metadata(
            conn,
            task_id,
            "claude",
            session_id=claude_uuid,
            session_name=f"{workspace}-claude",
        )
        set_session_metadata(
            conn,
            task_id,
            "codex",
            session_id=None,
            session_name=f"{workspace}-codex",
            discovery_marker=f"AGENT_MAILBOX_TASK_ID={task_id}",
            discovery_status="pending",
        )
        if args.tag:
            room_state_set(conn, task_id, "tags", list(args.tag))
        room_state_set(
            conn,
            task_id,
            "limits",
            {
                "max_rounds": args.max_rounds,
                "turn_timeout_seconds": 300,
                "max_peer_message_chars": 10000,
                "max_total_cost_usd": 5.0,
            },
        )
        room_state_set(
            conn,
            task_id,
            "usage",
            {"known_cost_usd": 0.0, "codex_input_tokens": 0, "codex_output_tokens": 0},
        )
        context = args.context or ""
        if args.context_file:
            context = args.context_file.read_text(encoding="utf-8")
        if context:
            send_message(
                conn,
                root=root,
                room_id=task_id,
                from_agent="user",
                to_agent="broadcast",
                kind="system",
                status="continue",
                summary="bootstrap context",
                body=context,
                next_turn=args.first_turn,
                increment_round=False,
            )
    finally:
        conn.close()
    return {"task_id": task_id, "workspace": workspace, "claude_session_id": claude_uuid, "root": str(root)}


def cmd_init(args) -> int:
    root = _root(args)
    return _emit(args, ok=True, data=_init_task(args, root))


def _launch_agent_panes(args, *, launch_relay: bool) -> dict[str, Any]:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        sessions = {
            row["agent"]: dict(row)
            for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
        }
        project_cwd = Path(room["project_cwd"])
        workspace = room["workspace"]
        skill_root = Path(__file__).resolve().parent.parent
        scripts = skill_root / "scripts"
        claude_cmd = [
            "cmd",
            "/c",
            str(scripts / "launch_claude_pane.cmd"),
            task_id,
            str(root),
            str(project_cwd),
            sessions["claude"]["session_id"],
            sessions["claude"]["session_name"],
        ]
        codex_cmd = [
            "cmd",
            "/c",
            str(scripts / "launch_codex_pane.cmd"),
            task_id,
            str(root),
            str(project_cwd),
            "",
        ]
        fake = os.environ.get("AGENT_MAILBOX_FAKE_PANE_IDS")
        if fake:
            parts = [int(x.strip()) for x in fake.split(",")]
            result = {"workspace": workspace, "claude_pane_id": parts[0], "codex_pane_id": parts[1], "spawned_at": utc_now()}
            relay_pane_id = parts[2] if launch_relay and len(parts) > 2 else None
            chat_pane_id = parts[3] if getattr(args, "with_chat", False) and len(parts) > 3 else None
            control_pane_id = parts[4] if getattr(args, "with_control_panel", False) and len(parts) > 4 else None
        else:
            from tui_launcher import attach_workspace_gui, ensure_mux_alive, find_codex, find_wezterm, launch_workspace, validate_workspace_startup

            _debug_timing("find_wezterm start")
            wez = find_wezterm()
            _debug_timing(f"find_wezterm done {wez}")
            _debug_timing("ensure_mux_alive start")
            ensure_mux_alive(wez)
            _debug_timing("ensure_mux_alive done")
            try:
                _debug_timing("find_stale_workspaces start")
                stale = _find_stale_workspaces(wezterm_exe=wez, rooms=_rooms_by_workspace(conn))
                _debug_timing(f"find_stale_workspaces done count={len(stale)}")
                if stale:
                    names = [entry["workspace"] for entry in stale[:3]]
                    suffix = f" and {len(stale) - 3} more" if len(stale) > 3 else ""
                    print(
                        f"warning: {len(stale)} stale agent-mailbox workspaces detected. "
                        f"({', '.join(names)}{suffix}). "
                        "Run 'mailbox.py reap-stale-workspaces' to clean up.",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(f"warning: stale workspace check failed: {exc}", file=sys.stderr)
            _debug_timing("find_codex start")
            codex_exe = find_codex()
            _debug_timing(f"find_codex done {codex_exe}")
            env = os.environ.copy()
            if codex_exe is not None:
                env["AGENT_MAILBOX_CODEX_EXE"] = str(codex_exe)
            _debug_timing("launch_workspace start")
            result = launch_workspace(
                wezterm_exe=wez,
                workspace=workspace,
                cwd=project_cwd,
                claude_cmd=claude_cmd,
                codex_cmd=codex_cmd,
                env=env,
            )
            _debug_timing(f"launch_workspace done {result}")
            relay_pane_id = None
            chat_pane_id = None
            control_pane_id = None
            _debug_timing("validate_workspace_startup start")
            startup = validate_workspace_startup(
                wezterm_exe=wez,
                workspace=workspace,
                claude_pane_id=int(result["claude_pane_id"]),
                codex_pane_id=int(result["codex_pane_id"]),
            )
            _debug_timing(f"validate_workspace_startup done ready={startup.get('ready')}")
            if launch_relay:
                from pane_control import build_split_argv

                if startup["ready"]:
                    relay_cmd = [
                        "cmd",
                        "/c",
                        str(scripts / "launch_relay_pane.cmd"),
                        task_id,
                        str(root),
                        str(project_cwd),
                        str(scripts / "mailbox.py"),
                    ]
                    if getattr(args, "max_iters", None) is not None:
                        relay_cmd.append(str(args.max_iters))
                    rv = subprocess.run(
                        build_split_argv(
                            wezterm_exe=wez,
                            source_pane_id=result["codex_pane_id"],
                            direction="bottom",
                            percent=25,
                            cwd=project_cwd,
                            cmd=relay_cmd,
                        ),
                        capture_output=True,
                        text=True,
                        stdin=subprocess.DEVNULL,
                    )
                    rv.check_returncode()
                    text = rv.stdout.strip()
                    relay_pane_id = int(text) if text.isdigit() else None
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
                        (_startup_not_ready_reason(startup), task_id),
                    )
                    conn.execute("COMMIT")
            if getattr(args, "with_chat", False):
                chat_cmd = [
                    "cmd",
                    "/c",
                    str(scripts / "launch_chat_pane.cmd"),
                    task_id,
                    str(root),
                    str(project_cwd),
                    str(scripts / "mailbox.py"),
                    str(getattr(args, "chat_poll_interval_s", 1.5)),
                ]
                chat_pane_id = _spawn_workspace_tab(wezterm_exe=wez, workspace=workspace, cwd=project_cwd, cmd=chat_cmd)
            if getattr(args, "with_control_panel", False):
                control_cmd = [
                    "cmd",
                    "/c",
                    str(scripts / "launch_control_panel.cmd"),
                    task_id,
                    str(root),
                    str(project_cwd),
                    str(scripts / "mailbox.py"),
                ]
                control_pane_id = _spawn_workspace_tab(wezterm_exe=wez, workspace=workspace, cwd=project_cwd, cmd=control_cmd)
            _debug_timing("attach_workspace_gui start")
            attach_workspace_gui(wez, workspace, project_cwd)
            _debug_timing("attach_workspace_gui done")
            result["startup"] = startup
        set_pane(conn, task_id, "claude", pane_id=result["claude_pane_id"])
        set_pane(conn, task_id, "codex", pane_id=result["codex_pane_id"])
        if relay_pane_id is not None:
            set_pane(conn, task_id, "relay", pane_id=relay_pane_id)
        if chat_pane_id is not None:
            set_pane(conn, task_id, "chat", pane_id=chat_pane_id)
        if control_pane_id is not None:
            set_pane(conn, task_id, "control", pane_id=control_pane_id)
        from codex_session_discovery import find_codex_session_id

        sessions_dir = os.environ.get("AGENT_MAILBOX_CODEX_SESSIONS_DIR")
        discovery = find_codex_session_id(
            task_id=task_id,
            project_cwd=project_cwd,
            sessions_root=Path(sessions_dir) if sessions_dir else None,
        )
        update_codex_discovery(
            conn,
            task_id,
            session_id=discovery["session_id"],
            status=discovery["status"],
            scanned_files=discovery["scanned_files"],
            attempted_at=discovery["attempted_at"],
        )
        return {
            "task_id": task_id,
            "workspace": workspace,
            "claude_pane_id": result["claude_pane_id"],
            "codex_pane_id": result["codex_pane_id"],
            "relay_pane_id": relay_pane_id,
            "chat_pane_id": chat_pane_id,
            "control_pane_id": control_pane_id,
            "codex_session_id": discovery["session_id"],
            "codex_discovery_status": discovery["status"],
            "startup": result.get("startup"),
        }
    finally:
        conn.close()


def cmd_launch_tui(args) -> int:
    return _emit(args, ok=True, data=_launch_agent_panes(args, launch_relay=False))


def cmd_start(args) -> int:
    root = _root(args)
    init_data = _init_task(args, root)
    args.task_id = init_data["task_id"]
    launch_data = _launch_agent_panes(args, launch_relay=True)
    return _emit(args, ok=True, data={**init_data, **launch_data})


def cmd_tui_relay(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
    finally:
        conn.close()
    from tui_launcher import find_wezterm
    if os.environ.get("AGENT_MAILBOX_RELAY_VERSION") == "2":
        from tui_relay_v2 import run_watcher_loop
    else:
        from tui_relay import run_watcher_loop

    result = run_watcher_loop(
        root=root,
        task_id=task_id,
        wezterm_exe=find_wezterm(),
        poll_interval_s=args.poll_interval_s,
        max_iters=args.max_iters,
    )
    return _emit(args, ok=True, data={"task_id": task_id, "exit_reason": result})


def cmd_trigger(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        pane = conn.execute(
            "SELECT pane_id FROM panes WHERE room_id=? AND pane_role=?",
            (task_id, args.agent),
        ).fetchone()
        if pane is None or pane["pane_id"] is None:
            return _emit(args, ok=False, error=f"no pane bound for {args.agent}")
        participants = [row["agent"] for row in conn.execute("SELECT agent FROM participants WHERE room_id=?", (task_id,))]
        peer = peer_for(participants, args.agent)
    finally:
        conn.close()
    from tui_launcher import find_wezterm
    from tui_relay import send_trigger

    ok = send_trigger(wezterm_exe=find_wezterm(), pane_id=int(pane["pane_id"]), agent=args.agent, peer=peer, task_id=task_id, root=root)
    return _emit(args, ok=ok, data={"task_id": task_id, "agent": args.agent}, error=None if ok else "send-text failed")


def cmd_inject(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        room = conn.execute("SELECT turn FROM rooms WHERE id=?", (task_id,)).fetchone()
        target = room["turn"] if args.target == "next" else args.target
        if target is None:
            return _emit(args, ok=False, error="no current turn; nothing to inject to")
        body = args.content if args.content is not None else sys.stdin.read()
        rv = send_message(
            conn,
            root=root,
            room_id=task_id,
            from_agent="user",
            to_agent=target,
            kind="inject",
            status="continue",
            summary=args.summary or "user injection",
            body=body,
            increment_round=False,
            next_turn=target,
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE rooms SET status='waiting', blocked_reason=NULL WHERE id=? AND status='blocked'", (task_id,))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _emit(args, ok=True, data={"task_id": task_id, "target": target, "message_id": rv["message_id"]})


def cmd_post(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        if args.body is not None:
            body = args.body
        elif args.body_file:
            body = args.body_file.read_text(encoding="utf-8")
        else:
            body = sys.stdin.read()
        participants = {str(row["agent"]).strip().lower() for row in conn.execute("SELECT agent FROM participants WHERE room_id=?", (task_id,)).fetchall()}
        protocol_warnings = lint_protocol_header(status=args.status, body=body, participants=participants)
        rv = send_message(
            conn,
            root=root,
            room_id=task_id,
            from_agent=args.from_agent,
            to_agent=args.to_agent,
            kind="message",
            status=args.status,
            summary=args.summary,
            body=body,
            blocked_reason=args.blocked_reason,
            next_turn=args.next_turn,
        )
    finally:
        conn.close()
    return _emit(args, ok=True, data={"task_id": task_id, "message_id": rv["message_id"], "warnings": protocol_warnings})


def cmd_show(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        room = dict(conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone())
        rows = conn.execute(
            "SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?",
            (task_id, args.tail),
        ).fetchall()
        messages = []
        for row in reversed(rows):
            msg = dict(row)
            if args.body:
                msg["body"] = get_message_body(root, msg["id"])
            messages.append(msg)
    finally:
        conn.close()
    return _emit(args, ok=True, data={"room": room, "messages": messages})


def _connect_readonly(root: Path) -> sqlite3.Connection:
    path = root / "agent-chat.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"mailbox DB not found: {path}")
    uri = path.resolve().as_posix()
    if os.name == "nt" and not uri.startswith("/"):
        uri = "/" + uri
    conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _message_body_from_row(root: Path, row: sqlite3.Row) -> str:
    if row["body_text"] is not None:
        return row["body_text"]
    if row["body_path"]:
        return (root / row["room_id"] / row["body_path"]).read_text(encoding="utf-8")
    return ""


def _ansi(text: str, code: str, *, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _format_chat_message(root: Path, row: sqlite3.Row, *, color: bool) -> str:
    author_code = {"claude": "34", "codex": "32", "user": "33"}.get(row["from_agent"], "2")
    author = _ansi(row["from_agent"], author_code, enabled=color)
    target = row["to_agent"] or "broadcast"
    header = f"[{row['id']}] {row['created_at']} {author} -> {target} [{row['status']}] {row['summary'] or ''}".rstrip()
    body = _message_body_from_row(root, row).rstrip()
    if body:
        indented = "\n".join(f"  {line}" if line else "" for line in body.splitlines())
        return f"{header}\n\n{indented}\n"
    return f"{header}\n"


def _watch_chat_once(conn: sqlite3.Connection, root: Path, task_id: str, *, after_id: int, color: bool) -> tuple[int, str, str]:
    room = conn.execute("SELECT status FROM rooms WHERE id=?", (task_id,)).fetchone()
    if room is None:
        raise KeyError(f"room {task_id} not found")
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id ASC",
        (task_id, after_id),
    ).fetchall()
    chunks = []
    seen = after_id
    for row in rows:
        chunks.append(_format_chat_message(root, row, color=color))
        seen = max(seen, int(row["id"]))
    return seen, room["status"], "\n".join(chunks)


def cmd_watch_chat(args) -> int:
    root = _root(args)
    conn = _connect_readonly(root)
    try:
        task_id = _resolve(conn, args.task_id)
        seen = max(0, args.from_message_id)
        terminal_seen_at: float | None = None
        color = bool(sys.stdout.isatty() and not args.no_color)
        iteration = 0
        print("[agent-mailbox] read-only transcript view; input is ignored. Use control panel for actions.", flush=True)
        while True:
            seen, status, text = _watch_chat_once(conn, root, task_id, after_id=seen, color=color)
            if text:
                print(text, end="\n", flush=True)
            if status in TERMINAL_ROOM_STATUSES:
                if terminal_seen_at is None:
                    terminal_seen_at = time.monotonic()
                    print(f"[agent-mailbox] room terminal: {status}", flush=True)
                elif time.monotonic() - terminal_seen_at >= args.terminal_grace_s:
                    return 0
            iteration += 1
            if args.max_iters is not None and iteration >= args.max_iters:
                return 0
            time.sleep(args.poll_interval_s)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


def _run_mailbox_cli(args, *subargs: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *subargs, "--root", str(_root(args)), "--task-id", args.task_id, "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _read_panel_state(root: Path, task_id: str) -> dict[str, Any]:
    conn = _connect_readonly(root)
    try:
        resolved = _resolve(conn, task_id)
        room = dict(conn.execute("SELECT * FROM rooms WHERE id=?", (resolved,)).fetchone())
        relay = conn.execute("SELECT * FROM tui_relay_state WHERE room_id=?", (resolved,)).fetchone()
        sessions = {
            row["agent"]: dict(row)
            for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (resolved,)).fetchall()
        }
        panes = {row["pane_role"]: row["pane_id"] for row in conn.execute("SELECT * FROM panes WHERE room_id=?", (resolved,)).fetchall()}
        latest = conn.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT 1", (resolved,)).fetchone()
        pending_user_injections = 0
        if room["turn"]:
            pending_user_injections = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM messages m "
                    "LEFT JOIN receipts r ON r.message_id=m.id AND r.agent=? "
                    "WHERE m.room_id=? AND m.from_agent='user' "
                    "AND m.kind != 'system' "
                    "AND (m.to_agent=? OR m.to_agent='broadcast' OR m.to_agent IS NULL) "
                    "AND r.ack_at IS NULL",
                    (room["turn"], resolved, room["turn"]),
                ).fetchone()["n"]
            )
        return {
            "task_id": resolved,
            "root": str(root),
            "room": room,
            "relay": dict(relay) if relay else {},
            "sessions": sessions,
            "panes": panes,
            "latest": dict(latest) if latest else None,
            "pending_user_injections": pending_user_injections,
        }
    finally:
        conn.close()


def _format_panel_status(state: dict[str, Any]) -> str:
    room = state["room"]
    relay = state["relay"]
    latest = state["latest"] or {}
    sessions = state["sessions"]
    panes = state["panes"]
    lines = [
        "Agent Mailbox Control Panel",
        "",
        f"Task: {state['task_id']}",
        f"Root: {state['root']}",
        f"Status: {room['status']}    Turn: {room['turn']}    Round: {room['round']}    Last message: {room['last_message_id']}",
        f"Paused: {bool(relay.get('paused', 0))}    Reason: {relay.get('pause_reason') or ''}",
        f"Watcher: pid={relay.get('watcher_pid') or '<none>'}    alive={pid_exists(relay.get('watcher_pid'))}",
        f"Pending user injections: {state.get('pending_user_injections', 0)}",
        f"Panes: {', '.join(f'{k}={v}' for k, v in sorted(panes.items())) or '(none)'}",
        f"Codex discovery: {sessions.get('codex', {}).get('discovery_status')}    Codex session: {sessions.get('codex', {}).get('session_id') or '<none>'}",
        "",
        f"Latest: [{latest.get('id', '-')}] {latest.get('from_agent', '-')} -> {latest.get('to_agent') or '-'} [{latest.get('status', '-')}] {latest.get('summary') or ''}",
        "",
        "Commands: r refresh | p pause | c resume | i inject | a agent actions | d rediscover Codex | s stop | q quit | ? help",
    ]
    if relay.get("watcher_pid") and not pid_exists(relay.get("watcher_pid")) and room["status"] not in TERMINAL_ROOM_STATUSES and not bool(relay.get("paused", 0)):
        lines.append("WARNING: relay watcher PID is dead while task is unpaused; run resume or restart the task watcher.")
    if room["status"] in TERMINAL_ROOM_STATUSES:
        lines.append("Room is terminal; mutation commands are disabled.")
    return "\n".join(lines)


def _panel_print_status(args) -> None:
    print(_format_panel_status(_read_panel_state(_root(args), args.task_id)))


def _panel_cli(args, *subargs: str) -> bool:
    rv = _run_mailbox_cli(args, *subargs)
    text = (rv.stdout or rv.stderr or "").strip()
    if text:
        print(text)
    return rv.returncode == 0


def _panel_pause(args, reason: str = "control_panel") -> bool:
    return _panel_cli(args, "pause", "--reason", reason)


def _panel_resume(args) -> bool:
    return _panel_cli(args, "resume")


def _panel_inject(args, content: str) -> bool:
    return _panel_cli(args, "inject", "--target", "next", "--summary", "control panel injection", "--content", content)


def _panel_stop(args) -> bool:
    return _panel_cli(args, "stop")


def _panel_rediscover_codex(args) -> bool:
    return _panel_cli(args, "repair", "--rediscover-codex")


def _panel_restart_agent(args, agent: str) -> bool:
    return _panel_cli(args, "repair", "--restart-agent", agent)


def _panel_rebind_pane(args, agent: str, pane_id: str) -> bool:
    if agent not in {"claude", "codex", "relay"}:
        print("Panel rebind supports claude, codex, and relay only. Use the CLI for other pane roles.")
        return False
    return _panel_cli(args, "repair", "--rebind-pane", "--agent", agent, "--pane-id", pane_id)


def _live_workspace_panes(root: Path, task_id: str) -> list[dict[str, Any]]:
    state = _read_panel_state(root, task_id)
    workspace = state["room"]["workspace"]
    from pane_control import build_list_argv
    from tui_launcher import find_wezterm, lookup_pane, parse_wezterm_list

    rv = subprocess.run(
        build_list_argv(wezterm_exe=find_wezterm()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    rv.check_returncode()
    return lookup_pane(parse_wezterm_list(rv.stdout), workspace=workspace)


def _panel_rebind_pane_interactive(args) -> None:
    try:
        panes = _live_workspace_panes(_root(args), args.task_id)
    except Exception as exc:
        print(f"Unable to list live WezTerm panes: {exc}")
        return
    if not panes:
        print("No live panes found in this task workspace.")
        return
    live_ids = {int(pane["pane_id"]) for pane in panes}
    print("Live pane candidates:")
    for pane in sorted(panes, key=lambda p: int(p["pane_id"])):
        title = pane.get("title") or pane.get("tty_name") or ""
        print(f"  {pane['pane_id']}  {title}")
    agent = input("agent (claude/codex/relay)> ").strip().lower()
    pane_id = input("pane id> ").strip()
    if agent not in {"claude", "codex", "relay"}:
        print("Invalid agent. Choose claude, codex, or relay.")
        return
    if not pane_id.isdigit() or int(pane_id) not in live_ids:
        print("Invalid pane id. Rebind refused because the pane is not live in this workspace.")
        return
    if _prompt_yes_no(f"Rebind {agent} to live pane {pane_id}?", default=False):
        _panel_rebind_pane(args, agent, pane_id)


def _prompt_yes_no(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _panel_bounce_agent(args, agent: str) -> None:
    state = _read_panel_state(_root(args), args.task_id)
    session_id = state["sessions"].get(agent, {}).get("session_id")
    discovery = state["sessions"].get(agent, {}).get("discovery_status")
    print(f"[{agent.capitalize()} bounce]")
    print(f"{agent} session_id: {session_id or '<none>'} ({discovery or 'n/a'})")
    if agent == "codex" and not session_id:
        print("Codex session id is missing; restart cannot run until rediscovery succeeds.")
        if not _prompt_yes_no("Run Codex rediscovery now?", default=False):
            return
        if not _panel_rediscover_codex(args):
            print("Rediscovery failed.")
            return
        state = _read_panel_state(_root(args), args.task_id)
        session_id = state["sessions"].get("codex", {}).get("session_id")
        if not session_id:
            print("Codex session is still missing. Codex may need to post once with the task marker.")
            return
        print(f"Codex discovered: {session_id}")
    if _prompt_yes_no("Pause relay before bounce?", default=True):
        _panel_pause(args, reason=f"control_panel_bounce_{agent}")
    if not _prompt_yes_no("Proceed with bounce?", default=False):
        print("Bounce cancelled.")
        return
    if _panel_restart_agent(args, agent) and _prompt_yes_no("Resume relay after?", default=True):
        _panel_resume(args)


def _panel_rediscover_then_bounce_codex(args) -> None:
    print("[Codex rediscover + bounce]")
    if _prompt_yes_no("Pause relay before rediscovery?", default=True):
        _panel_pause(args, reason="control_panel_rediscover_codex")
    if not _prompt_yes_no("Run rediscovery?", default=False):
        print("Rediscovery cancelled; relay remains paused.")
        return
    if not _panel_rediscover_codex(args):
        print("Rediscovery failed; relay remains paused.")
        return
    state = _read_panel_state(_root(args), args.task_id)
    session_id = state["sessions"].get("codex", {}).get("session_id")
    if not session_id:
        print("Codex session is still missing. Codex may need to post once with the task marker, or you may need to rebind/resume manually.")
        return
    print(f"Codex discovered: {session_id}")
    if _prompt_yes_no("Proceed with bounce?", default=False) and _panel_restart_agent(args, "codex"):
        if _prompt_yes_no("Resume relay after?", default=True):
            _panel_resume(args)


def _panel_agent_menu(args) -> None:
    while True:
        print(
            "\nAgent actions:\n"
            "1 bounce Claude in existing pane\n"
            "2 bounce Codex in existing pane\n"
            "3 rediscover Codex, then bounce Codex if discovered\n"
            "4 run resume to recreate missing panes\n"
            "5 rebind pane id manually\n"
            "q back"
        )
        choice = input("agent action> ").strip().lower()
        if choice == "1":
            _panel_bounce_agent(args, "claude")
        elif choice == "2":
            _panel_bounce_agent(args, "codex")
        elif choice == "3":
            _panel_rediscover_then_bounce_codex(args)
        elif choice == "4":
            _panel_resume(args)
        elif choice == "5":
            _panel_rebind_pane_interactive(args)
        elif choice == "q":
            return
        else:
            print("Unknown action.")


def _panel_help() -> None:
    print(
        "\nCommands:\n"
        "r  refresh status\n"
        "p  pause relay\n"
        "c  continue / resume\n"
        "i  inject free-form guidance\n"
        "a  agent actions / bounce submenu\n"
        "d  rediscover Codex session\n"
        "s  stop task, keep panes open\n"
        "q  quit control panel only\n"
        "?  help\n"
    )


def _panel_handle_command(args, command: str) -> bool:
    command = command.strip().lower()
    state = _read_panel_state(_root(args), args.task_id)
    terminal = state["room"]["status"] in TERMINAL_ROOM_STATUSES
    if command == "q":
        return False
    if command in {"r", ""}:
        _panel_print_status(args)
    elif command == "?":
        _panel_help()
    elif terminal:
        print("Room is terminal; mutation commands are disabled.")
    elif command == "p":
        _panel_pause(args)
    elif command == "c":
        _panel_resume(args)
    elif command == "i":
        content = input("Inject guidance> ").strip()
        if content and _prompt_yes_no("Send injection?", default=False):
            _panel_inject(args, content)
    elif command == "a":
        _panel_agent_menu(args)
    elif command == "d":
        _panel_rediscover_codex(args)
    elif command == "s":
        if _prompt_yes_no("Stop task and keep panes open?", default=False):
            _panel_stop(args)
    else:
        print("Unknown command. Press ? for help.")
    return True


def cmd_control_panel(args) -> int:
    if args.once:
        _panel_print_status(args)
        return 0
    iters = 0
    _panel_print_status(args)
    while True:
        if args.max_iters is not None and iters >= args.max_iters:
            return 0
        if args.commands:
            if iters >= len(args.commands):
                return 0
            command = args.commands[iters]
        else:
            command = input("control> ")
        iters += 1
        try:
            if not _panel_handle_command(args, command):
                return 0
        except Exception as exc:
            print(f"ERROR: {exc}")


def cmd_status(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        room = dict(conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone())
        relay = dict(conn.execute("SELECT * FROM tui_relay_state WHERE room_id=?", (task_id,)).fetchone())
        sessions = {
            row["agent"]: dict(row)
            for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
        }
        panes = {row["pane_role"]: row["pane_id"] for row in conn.execute("SELECT * FROM panes WHERE room_id=?", (task_id,))}
        watcher_alive = pid_exists(relay["watcher_pid"])
        watcher_dead_with_running_state = bool(
            relay["watcher_pid"]
            and not watcher_alive
            and room["status"] not in TERMINAL_ROOM_STATUSES
            and not bool(relay["paused"])
        )
        pending_user_injections = 0
        if room["turn"]:
            pending_user_injections = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM messages m "
                    "LEFT JOIN receipts r ON r.message_id=m.id AND r.agent=? "
                    "WHERE m.room_id=? AND m.from_agent='user' "
                    "AND m.kind != 'system' "
                    "AND (m.to_agent=? OR m.to_agent='broadcast' OR m.to_agent IS NULL) "
                    "AND r.ack_at IS NULL",
                    (room["turn"], task_id, room["turn"]),
                ).fetchone()["n"]
            )
        data = {
            "task_id": task_id,
            "status": room["status"],
            "turn": room["turn"],
            "round": room["round"],
            "last_message_id": room["last_message_id"],
            "paused": bool(relay["paused"]),
            "pause_reason": relay["pause_reason"],
            "watcher_pid": relay["watcher_pid"],
            "watcher_alive": watcher_alive,
            "watcher_dead_with_running_state": watcher_dead_with_running_state,
            "pending_user_injections": pending_user_injections,
            "panes": panes,
            "codex_discovery_status": sessions.get("codex", {}).get("discovery_status"),
        }
    finally:
        conn.close()
    return _emit(args, ok=True, data=data)


def _workspace_task_id(workspace: str) -> str | None:
    prefix = "agent-mailbox-"
    return workspace[len(prefix):] if workspace.startswith(prefix) else None


def _rooms_by_workspace(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row["workspace"]: dict(row)
        for row in conn.execute("SELECT id, workspace, status FROM rooms").fetchall()
    }


def _find_stale_workspaces(*, wezterm_exe: Path, rooms: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    from pane_control import build_list_argv
    from tui_launcher import parse_wezterm_list, run_wezterm_cli

    rv = run_wezterm_cli(
        build_list_argv(wezterm_exe=wezterm_exe),
        timeout=15,
    )
    if rv.returncode == 124:
        print(f"warning: unable to list WezTerm panes for stale cleanup: {rv.stderr.strip()}", file=sys.stderr)
        return []
    rv.check_returncode()
    panes = parse_wezterm_list(rv.stdout)
    by_workspace: dict[str, list[int]] = {}
    for pane in panes:
        workspace = str(pane.get("workspace") or "")
        if _workspace_task_id(workspace) is None:
            continue
        by_workspace.setdefault(workspace, []).append(int(pane["pane_id"]))

    stale: list[dict[str, Any]] = []
    for workspace, pane_ids in sorted(by_workspace.items()):
        room = rooms.get(workspace)
        reason = None
        if room is None:
            reason = "no_db_room"
        elif room["status"] in TERMINAL_ROOM_STATUSES:
            reason = f"terminal:{room['status']}"
        if reason is None:
            continue
        entry = {
            "workspace": workspace,
            "task_id": room["id"] if room else _workspace_task_id(workspace),
            "reason": reason,
            "pane_ids": pane_ids,
        }
        stale.append(entry)
    return stale


def cmd_reap_stale_workspaces(args) -> int:
    root = _root(args)
    init_db(root)
    conn = connect_db(root)
    try:
        rooms = _rooms_by_workspace(conn)
    finally:
        conn.close()

    from pane_control import build_kill_pane_argv
    from tui_launcher import find_wezterm, run_wezterm_cli

    wez = find_wezterm()
    stale = _find_stale_workspaces(wezterm_exe=wez, rooms=rooms)
    killed: list[int] = []
    for entry in stale:
        if args.yes:
            for pane_id in entry["pane_ids"]:
                kill = run_wezterm_cli(
                    build_kill_pane_argv(wezterm_exe=wez, pane_id=pane_id),
                    timeout=15,
                )
                kill.check_returncode()
                killed.append(pane_id)
    return _emit(args, ok=True, data={"dry_run": not args.yes, "stale_workspaces": stale, "killed_pane_ids": killed})


def cmd_dry_run(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        if room is None:
            return _emit(args, ok=False, error=f"room not found: {task_id}")
        turn = room["turn"]
        if not turn:
            return _emit(args, ok=False, error="room has no current turn")
        participants = [row["agent"] for row in conn.execute("SELECT agent FROM participants WHERE room_id=? ORDER BY agent", (task_id,)).fetchall()]
        peer = peer_for(participants, turn)
        from tui_relay import trigger_text

        text = trigger_text(agent=turn, peer=peer, task_id=task_id, root=root, first_turn=int(room["round"]) == 0)
    finally:
        conn.close()
    return _emit(args, ok=True, data={"task_id": task_id, "agent": turn, "peer": peer, "trigger_text": text})


ARCHIVE_TABLES = (
    "rooms",
    "participants",
    "agent_sessions",
    "panes",
    "tui_relay_state",
    "messages",
    "message_sources",
    "receipts",
    "room_state",
)


def _copy_table_rows(src: sqlite3.Connection, dst: sqlite3.Connection, *, table: str, room_id: str) -> int:
    if table == "receipts":
        rows = src.execute(
            "SELECT r.* FROM receipts r JOIN messages m ON m.id=r.message_id WHERE m.room_id=?",
            (room_id,),
        ).fetchall()
    elif table == "messages":
        rows = src.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id", (room_id,)).fetchall()
    elif table == "message_sources":
        rows = src.execute("SELECT * FROM message_sources WHERE room_id=? ORDER BY message_id", (room_id,)).fetchall()
    elif table == "rooms":
        rows = src.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchall()
    else:
        rows = src.execute(f"SELECT * FROM {table} WHERE room_id=?", (room_id,)).fetchall()
    if not rows:
        return 0
    columns = rows[0].keys()
    placeholders = ",".join("?" for _ in columns)
    names = ",".join(columns)
    dst.executemany(
        f"INSERT OR REPLACE INTO {table}({names}) VALUES({placeholders})",
        [tuple(row[col] for col in columns) for row in rows],
    )
    return len(rows)


def cmd_archive(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    archive_conn: sqlite3.Connection | None = None
    try:
        task_id = _resolve(conn, args.task_id)
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        if room is None:
            return _emit(args, ok=False, error=f"room not found: {task_id}")
        if room["status"] not in TERMINAL_ROOM_STATUSES and not args.force:
            return _emit(args, ok=False, error="archive requires a terminal room unless --force is set")
        if not args.yes:
            return _emit(args, ok=False, error="archive requires --yes")

        archive_root = (args.archive_root or (root / "archives")).resolve()
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_db = archive_root / f"{task_id}.sqlite"
        if archive_db.exists() and not args.overwrite:
            return _emit(args, ok=False, error=f"archive already exists: {archive_db}")
        if archive_db.exists():
            archive_db.unlink()

        archive_conn = sqlite3.connect(str(archive_db), isolation_level=None)
        archive_conn.row_factory = sqlite3.Row
        archive_conn.execute("PRAGMA foreign_keys=ON")
        archive_conn.executescript(DDL)
        archive_conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(1),),
        )

        copied: dict[str, int] = {}
        archive_conn.execute("BEGIN IMMEDIATE")
        for table in ARCHIVE_TABLES:
            copied[table] = _copy_table_rows(conn, archive_conn, table=table, room_id=task_id)
        archive_conn.execute("COMMIT")

        task_dir = root / task_id
        archive_task_dir = archive_root / task_id
        if archive_task_dir.exists():
            shutil.rmtree(archive_task_dir)
        if task_dir.exists():
            shutil.copytree(task_dir, archive_task_dir)
        transcript = archive_root / f"{task_id}.transcript.md"
        transcript.write_text(export_transcript_md(conn, root=root, room_id=task_id), encoding="utf-8", newline="\n")

        if task_dir.exists():
            shutil.rmtree(task_dir)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM receipts WHERE message_id IN (SELECT id FROM messages WHERE room_id=?)", (task_id,))
            conn.execute("DELETE FROM message_sources WHERE room_id=?", (task_id,))
            for table in ("room_state", "tui_relay_state", "panes", "agent_sessions", "participants", "messages"):
                conn.execute(f"DELETE FROM {table} WHERE room_id=?", (task_id,))
            conn.execute("DELETE FROM rooms WHERE id=?", (task_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return _emit(
            args,
            ok=True,
            data={
                "task_id": task_id,
                "archive_db": str(archive_db),
                "archive_dir": str(archive_task_dir),
                "transcript": str(transcript),
                "copied": copied,
            },
        )
    finally:
        if archive_conn is not None:
            archive_conn.close()
        conn.close()


def cmd_list(args) -> int:
    root = _root(args)
    init_db(root)
    conn = connect_db(root)
    try:
        rooms = list_rooms(conn, active_only=args.active_only)
    finally:
        conn.close()
    return _emit(args, ok=True, data={"rooms": rooms})


def cmd_export(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        text = export_transcript_md(conn, root=root, room_id=task_id)
    finally:
        conn.close()
    out = args.to or (root / task_id / "transcript.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    return _emit(args, ok=True, data={"task_id": task_id, "path": str(out)})


def cmd_ack(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        _resolve(conn, args.task_id)
        ack_message(conn, message_id=args.message_id, agent=args.agent)
    finally:
        conn.close()
    return _emit(args, ok=True, data={"message_id": args.message_id, "agent": args.agent})


def cmd_pause(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
            (args.reason or "user_paused", task_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _emit(args, ok=True, data={"task_id": task_id, "paused": True})


def _pick_active(conn) -> str:
    rows = list_rooms(conn, active_only=True)
    if len(rows) != 1:
        raise ValueError(f"expected one active task, found {len(rows)}")
    return rows[0]["id"]


def cmd_resume(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    actions: list[str] = []
    startup: dict[str, Any] | None = None
    try:
        task_id = _resolve(conn, args.task_id) if args.task_id else _pick_active(conn)
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        sessions = {
            row["agent"]: dict(row)
            for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
        }
        if not sessions["codex"]["session_id"]:
            return _emit(args, ok=False, error="missing codex session_id; run mailbox repair --rediscover-codex")
        panes_db = {row["pane_role"]: row["pane_id"] for row in conn.execute("SELECT * FROM panes WHERE room_id=?", (task_id,))}
        from tui_launcher import find_wezterm, parse_wezterm_list
        from pane_control import build_list_argv, build_spawn_argv, build_split_argv

        wez = find_wezterm()
        rv = subprocess.run(
            build_list_argv(wezterm_exe=wez),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
        live = parse_wezterm_list(rv.stdout) if rv.returncode == 0 else []
        live_ids = {int(pane["pane_id"]) for pane in live}
        workspace_alive = any(pane.get("workspace") == room["workspace"] for pane in live)
        if not workspace_alive:
            actions.append("relaunch_workspace")
        else:
            for role in ("claude", "codex"):
                if panes_db.get(role) is None or int(panes_db[role]) not in live_ids:
                    actions.append(f"recreate_{role}_pane")
        project_cwd = Path(room["project_cwd"])
        scripts = Path(__file__).resolve().parent
        if "relaunch_workspace" in actions:
            from tui_launcher import launch_workspace

            result = launch_workspace(
                wezterm_exe=wez,
                workspace=room["workspace"],
                cwd=project_cwd,
                claude_cmd=[
                    "cmd",
                    "/c",
                    str(scripts / "resume_claude_pane.cmd"),
                    task_id,
                    str(root),
                    str(project_cwd),
                    sessions["claude"]["session_id"],
                ],
                codex_cmd=[
                    "cmd",
                    "/c",
                    str(scripts / "resume_codex_pane.cmd"),
                    task_id,
                    str(root),
                    str(project_cwd),
                    sessions["codex"]["session_id"],
                ],
            )
            set_pane(conn, task_id, "claude", pane_id=result["claude_pane_id"])
            set_pane(conn, task_id, "codex", pane_id=result["codex_pane_id"])
        elif "recreate_codex_pane" in actions:
            source = panes_db.get("claude") or next(iter(live_ids))
            rv = subprocess.run(
                build_split_argv(
                    wezterm_exe=wez,
                    source_pane_id=int(source),
                    direction="right",
                    percent=50,
                    cwd=project_cwd,
                    cmd=[
                        "cmd",
                        "/c",
                        str(scripts / "resume_codex_pane.cmd"),
                        task_id,
                        str(root),
                        str(project_cwd),
                        sessions["codex"]["session_id"],
                    ],
                ),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            rv.check_returncode()
            if rv.stdout.strip().isdigit():
                set_pane(conn, task_id, "codex", pane_id=int(rv.stdout.strip()))
        elif "recreate_claude_pane" in actions:
            rv = subprocess.run(
                build_spawn_argv(
                    wezterm_exe=wez,
                    workspace=room["workspace"],
                    cwd=project_cwd,
                    cmd=[
                        "cmd",
                        "/c",
                        str(scripts / "resume_claude_pane.cmd"),
                        task_id,
                        str(root),
                        str(project_cwd),
                        sessions["claude"]["session_id"],
                    ],
                    new_window=False,
                ),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            rv.check_returncode()
            if rv.stdout.strip().isdigit():
                set_pane(conn, task_id, "claude", pane_id=int(rv.stdout.strip()))
        startup = _validate_task_startup(conn, task_id=task_id, wezterm_exe=wez)
        _apply_relay_startup_gate(conn, task_id=task_id, startup=startup, clear_if_ready=True)
        if startup.get("ready") and room["status"] == "stopped":
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE rooms SET status='waiting' WHERE id=?", (task_id,))
            conn.execute("COMMIT")
    finally:
        conn.close()
    return _emit(args, ok=True, data={"task_id": task_id, "actions": actions, "startup": startup})


def cmd_stop(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    pane_ids: list[int] = []
    try:
        task_id = _resolve(conn, args.task_id)
        pane_ids = [int(row["pane_id"]) for row in conn.execute("SELECT pane_id FROM panes WHERE room_id=? AND pane_id IS NOT NULL", (task_id,))]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE rooms SET status='stopped' WHERE id=?", (task_id,))
        conn.execute("UPDATE tui_relay_state SET paused=1, pause_reason='user_stopped' WHERE room_id=?", (task_id,))
        conn.execute("COMMIT")
    finally:
        conn.close()
    if args.close_panes:
        if not args.yes:
            return _emit(args, ok=False, error="--close-panes requires --yes")
        from tui_launcher import find_wezterm, run_wezterm_cli
        from pane_control import build_kill_pane_argv

        wez = find_wezterm()
        for pane_id in pane_ids:
            run_wezterm_cli(
                build_kill_pane_argv(wezterm_exe=wez, pane_id=pane_id),
                timeout=15,
            )
    return _emit(args, ok=True, data={"task_id": task_id, "status": "stopped"})


def cmd_repair(args) -> int:
    root = _root(args)
    conn = connect_db(root)
    try:
        task_id = _resolve(conn, args.task_id)
        data: dict[str, Any] = {"task_id": task_id}
        if args.rebind_pane:
            if not args.agent or args.pane_id is None:
                return _emit(args, ok=False, error="--rebind-pane requires --agent and --pane-id")
            set_pane(conn, task_id, args.agent, pane_id=args.pane_id)
            data["rebound"] = {args.agent: args.pane_id}
        if args.rediscover_codex:
            room = conn.execute("SELECT project_cwd FROM rooms WHERE id=?", (task_id,)).fetchone()
            from codex_session_discovery import find_codex_session_id

            discovery = find_codex_session_id(task_id=task_id, project_cwd=Path(room["project_cwd"]))
            update_codex_discovery(
                conn,
                task_id,
                session_id=discovery["session_id"],
                status=discovery["status"],
                scanned_files=discovery["scanned_files"],
                attempted_at=discovery["attempted_at"],
            )
            data["codex_discovery_status"] = discovery["status"]
            data["codex_session_id"] = discovery["session_id"]
        if args.restart_agent:
            agent = args.restart_agent
            session = conn.execute(
                "SELECT session_id FROM agent_sessions WHERE room_id=? AND agent=?",
                (task_id, agent),
            ).fetchone()
            if not session or not session["session_id"]:
                return _emit(args, ok=False, error=f"missing {agent} session_id")
            pane = conn.execute(
                "SELECT pane_id FROM panes WHERE room_id=? AND pane_role=?",
                (task_id, agent),
            ).fetchone()
            if not pane or pane["pane_id"] is None:
                return _emit(args, ok=False, error=f"no pane bound for {agent}; use --rebind-pane first")
            from pane_control import build_send_text_argv
            from tui_launcher import find_wezterm

            cmd = (
                f'claude --resume "{session["session_id"]}"'
                if agent == "claude"
                else f'codex resume "{session["session_id"]}"'
            )
            rv = subprocess.run(
                build_send_text_argv(
                    wezterm_exe=find_wezterm(),
                    pane_id=int(pane["pane_id"]),
                    text=cmd + "\r",
                    no_paste=True,
                ),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            if rv.returncode != 0:
                return _emit(args, ok=False, error=f"failed to send restart command to {agent} pane: {rv.stderr.strip()}")
            data["restarted_agent"] = agent
            startup = _validate_task_startup(conn, task_id=task_id, wezterm_exe=find_wezterm())
            _apply_relay_startup_gate(conn, task_id=task_id, startup=startup, clear_if_ready=False)
            data["startup"] = startup
        if args.use_last_codex_session:
            if not args.yes:
                return _emit(args, ok=False, error="--use-last-codex-session requires --yes")
            return _emit(args, ok=False, error="automatic codex resume --last is intentionally unsupported")
    finally:
        conn.close()
    return _emit(args, ok=True, data=data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailbox.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    _common(p)
    p.add_argument("--task-id")
    p.add_argument("--prefix", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--goal", required=True)
    p.add_argument("--project-cwd", required=True)
    p.add_argument("--first-turn", choices=["claude", "codex"], default="claude")
    p.add_argument("--tag", action="append")
    p.add_argument("--parent-task-id")
    p.add_argument("--exit-condition")
    p.add_argument("--max-rounds", type=int, default=30)
    p.add_argument("--context")
    p.add_argument("--context-file", type=Path)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("start")
    _common(p)
    p.add_argument("--task-id")
    p.add_argument("--prefix", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--goal", required=True)
    p.add_argument("--project-cwd", required=True)
    p.add_argument("--first-turn", choices=["claude", "codex"], default="claude")
    p.add_argument("--tag", action="append")
    p.add_argument("--parent-task-id")
    p.add_argument("--exit-condition")
    p.add_argument("--max-rounds", type=int, default=30)
    p.add_argument("--max-iters", type=int)
    _chat_flags(p)
    _control_flags(p)
    p.add_argument("--context")
    p.add_argument("--context-file", type=Path)
    p.set_defaults(func=cmd_start)

    for name, func in [("launch-tui", cmd_launch_tui), ("tui-relay", cmd_tui_relay), ("trigger", cmd_trigger)]:
        p = sub.add_parser(name)
        _common(p)
        p.add_argument("--task-id", required=True)
        if name == "launch-tui":
            _chat_flags(p)
            _control_flags(p)
        if name == "tui-relay":
            p.add_argument("--poll-interval-s", type=float, default=2.0)
            p.add_argument("--max-iters", type=int)
        if name == "trigger":
            p.add_argument("--agent", choices=["claude", "codex"], required=True)
        p.set_defaults(func=func)

    p = sub.add_parser("inject")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--target", default="next")
    p.add_argument("--summary")
    p.add_argument("--content")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("post")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--from", dest="from_agent", required=True)
    p.add_argument("--to", dest="to_agent", required=True)
    p.add_argument("--status", choices=["continue", "blocked", "final", "error"], required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--body")
    p.add_argument("--body-file", type=Path)
    p.add_argument("--blocked-reason")
    p.add_argument("--next-turn")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("show")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--tail", type=int, default=5)
    p.add_argument("--body", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("watch-chat")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--poll-interval-s", type=float, default=1.5)
    p.add_argument("--from-message-id", type=int, default=0)
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--max-iters", type=int)
    p.add_argument("--terminal-grace-s", type=float, default=30.0)
    p.set_defaults(func=cmd_watch_chat)

    p = sub.add_parser("dry-run")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("control-panel")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--once", action="store_true")
    p.add_argument("--max-iters", type=int)
    p.add_argument("--commands", nargs="*")
    p.set_defaults(func=cmd_control_panel)

    p = sub.add_parser("status")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reap-stale-workspaces")
    _common(p)
    p.add_argument("--yes", action="store_true", help="kill stale WezTerm panes; without this, only report what would be killed")
    p.set_defaults(func=cmd_reap_stale_workspaces)

    p = sub.add_parser("list")
    _common(p)
    p.add_argument("--active-only", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--to", type=Path)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("archive")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--archive-root", type=Path)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="allow archiving a non-terminal task")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("ack")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--message-id", type=int, required=True)
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("pause")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume")
    _common(p)
    p.add_argument("--task-id")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("stop")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--close-panes", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("repair")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--rediscover-codex", action="store_true")
    p.add_argument("--use-last-codex-session", action="store_true")
    p.add_argument("--restart-agent", choices=["claude", "codex"])
    p.add_argument("--rebind-pane", action="store_true")
    p.add_argument("--agent", choices=["claude", "codex", "relay"])
    p.add_argument("--pane-id", type=int)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_repair)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        return _emit(args, ok=False, error=str(exc))


if __name__ == "__main__":
    sys.exit(main())
