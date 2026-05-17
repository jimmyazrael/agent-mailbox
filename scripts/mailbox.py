#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from agent_chat import (
    ack_message,
    add_participant,
    connect_db,
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


def lint_protocol_header(*, status: str, body: str) -> list[str]:
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


def _common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--root", type=Path)
    subparser.add_argument("--format", choices=["human", "json"], default="human")


def _chat_flags(subparser: argparse.ArgumentParser) -> None:
    group = subparser.add_mutually_exclusive_group()
    group.add_argument("--with-chat", dest="with_chat", action="store_true", help="open the read-only chat transcript view (default)")
    group.add_argument("--no-chat", dest="with_chat", action="store_false", help="do not open the chat transcript view")
    subparser.set_defaults(with_chat=True)
    subparser.add_argument("--chat-poll-interval-s", type=float, default=1.5)


def _resolve(conn, task_id: str) -> str:
    return resolve_task_id(conn, task_id)


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
        else:
            from tui_launcher import ensure_mux_alive, find_wezterm, launch_workspace

            wez = find_wezterm()
            ensure_mux_alive(wez)
            result = launch_workspace(
                wezterm_exe=wez,
                workspace=workspace,
                cwd=project_cwd,
                claude_cmd=claude_cmd,
                codex_cmd=codex_cmd,
            )
            relay_pane_id = None
            chat_pane_id = None
            if launch_relay:
                from pane_control import build_split_argv

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
                )
                rv.check_returncode()
                text = rv.stdout.strip()
                relay_pane_id = int(text) if text.isdigit() else None
            if getattr(args, "with_chat", False):
                from pane_control import build_list_argv, build_spawn_argv
                from tui_launcher import lookup_pane, parse_wezterm_list

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
                list_rv = subprocess.run(
                    build_list_argv(wezterm_exe=wez),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                list_rv.check_returncode()
                workspace_panes = lookup_pane(parse_wezterm_list(list_rv.stdout), workspace=workspace)
                if not workspace_panes:
                    raise RuntimeError(f"workspace not found for chat pane: {workspace}")
                window_id = int(workspace_panes[0]["window_id"])
                rv = subprocess.run(
                    build_spawn_argv(
                        wezterm_exe=wez,
                        workspace=workspace,
                        cwd=project_cwd,
                        cmd=chat_cmd,
                        new_window=False,
                        window_id=window_id,
                    ),
                    capture_output=True,
                    text=True,
                )
                rv.check_returncode()
                text = rv.stdout.strip()
                chat_pane_id = int(text) if text.isdigit() else None
        set_pane(conn, task_id, "claude", pane_id=result["claude_pane_id"])
        set_pane(conn, task_id, "codex", pane_id=result["codex_pane_id"])
        if relay_pane_id is not None:
            set_pane(conn, task_id, "relay", pane_id=relay_pane_id)
        if chat_pane_id is not None:
            set_pane(conn, task_id, "chat", pane_id=chat_pane_id)
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
            "codex_session_id": discovery["session_id"],
            "codex_discovery_status": discovery["status"],
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
        protocol_warnings = lint_protocol_header(status=args.status, body=body)
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
        data = {
            "task_id": task_id,
            "status": room["status"],
            "turn": room["turn"],
            "round": room["round"],
            "last_message_id": room["last_message_id"],
            "paused": bool(relay["paused"]),
            "pause_reason": relay["pause_reason"],
            "watcher_alive": pid_exists(relay["watcher_pid"]),
            "panes": panes,
            "codex_discovery_status": sessions.get("codex", {}).get("discovery_status"),
        }
    finally:
        conn.close()
    return _emit(args, ok=True, data=data)


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
    try:
        task_id = _resolve(conn, args.task_id) if args.task_id else _pick_active(conn)
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        sessions = {
            row["agent"]: dict(row)
            for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
        }
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
        if ("relaunch_workspace" in actions or "recreate_codex_pane" in actions) and not sessions["codex"]["session_id"]:
            return _emit(args, ok=False, error="missing codex session_id; run mailbox repair --rediscover-codex")
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
            )
            rv.check_returncode()
            if rv.stdout.strip().isdigit():
                set_pane(conn, task_id, "claude", pane_id=int(rv.stdout.strip()))
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tui_relay_state SET paused=0, pause_reason=NULL WHERE room_id=?",
            (task_id,),
        )
        if room["status"] == "stopped":
            conn.execute("UPDATE rooms SET status='waiting' WHERE id=?", (task_id,))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _emit(args, ok=True, data={"task_id": task_id, "actions": actions})


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
        from tui_launcher import find_wezterm
        from pane_control import build_send_text_argv

        wez = find_wezterm()
        for pane_id in pane_ids:
            subprocess.run(build_send_text_argv(wezterm_exe=wez, pane_id=pane_id, text="\x03exit\r"), capture_output=True, text=True)
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
            )
            if rv.returncode != 0:
                return _emit(args, ok=False, error=f"failed to send restart command to {agent} pane: {rv.stderr.strip()}")
            data["restarted_agent"] = agent
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
    p.add_argument("--context")
    p.add_argument("--context-file", type=Path)
    p.set_defaults(func=cmd_start)

    for name, func in [("launch-tui", cmd_launch_tui), ("tui-relay", cmd_tui_relay), ("trigger", cmd_trigger)]:
        p = sub.add_parser(name)
        _common(p)
        p.add_argument("--task-id", required=True)
        if name == "launch-tui":
            _chat_flags(p)
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

    p = sub.add_parser("status")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list")
    _common(p)
    p.add_argument("--active-only", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export")
    _common(p)
    p.add_argument("--task-id", required=True)
    p.add_argument("--to", type=Path)
    p.set_defaults(func=cmd_export)

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
