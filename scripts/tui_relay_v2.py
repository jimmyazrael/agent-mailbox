from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from agent_chat import connect_db
from mailbox_lib import TERMINAL_STATUSES, utc_now
from outbox import OutboxError, import_outbox_messages, next_outbox_path
from pane_control import build_activate_pane_argv, build_list_argv, build_send_text_argv
from tui_launcher import lookup_pane, parse_wezterm_list


def doorbell_text(*, agent: str, peer: str, task_id: str, root: Path) -> str:
    write_path = next_outbox_path(root, task_id, agent)
    return (
        f"{peer.capitalize()} has replied on agent-mailbox task {task_id}.\n\n"
        "Read the latest reply in the chat monitor pane.\n\n"
        "To respond:\n"
        f"1. Write your reply to: {write_path}\n"
        "2. Use frontmatter: from, to, status, summary; then body.\n"
        "3. End with: <!-- AGENT-MAILBOX:DONE -->\n"
        "4. Stop.\r"
    )


def first_turn_text(*, agent: str, task_id: str, root: Path) -> str:
    write_path = next_outbox_path(root, task_id, agent)
    return (
        f"You have the first turn on agent-mailbox task {task_id}.\n\n"
        "Read the bootstrap context in the chat monitor pane.\n\n"
        "To respond:\n"
        f"1. Write your reply to: {write_path}\n"
        "2. Use frontmatter: from, to, status, summary; then body.\n"
        "3. End with: <!-- AGENT-MAILBOX:DONE -->\n"
        "4. Stop.\r"
    )


def send_doorbell(*, wezterm_exe: Path, pane_id: int, text: str) -> bool:
    subprocess.run(
        build_activate_pane_argv(wezterm_exe=wezterm_exe, pane_id=pane_id),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    rv_text = subprocess.run(
        build_send_text_argv(wezterm_exe=wezterm_exe, pane_id=pane_id, text=text.rstrip("\r"), no_paste=True),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if rv_text.returncode != 0:
        return False
    time.sleep(0.5)
    rv_enter = subprocess.run(
        build_send_text_argv(wezterm_exe=wezterm_exe, pane_id=pane_id, text="\r\n", no_paste=True),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return rv_enter.returncode == 0


def _pane_alive(wezterm_exe: Path, pane_id: int) -> bool:
    rv = subprocess.run(
        build_list_argv(wezterm_exe=wezterm_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if rv.returncode != 0:
        return False
    try:
        panes = parse_wezterm_list(rv.stdout)
    except (ValueError, TypeError):
        return False
    return bool(lookup_pane(panes, pane_id=pane_id))


def _pause_relay(conn, task_id: str, reason: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
        (reason, task_id),
    )
    conn.execute("COMMIT")


def _peer_for_agent(conn, room_id: str, agent: str) -> str:
    rows = conn.execute("SELECT agent FROM participants WHERE room_id=? ORDER BY agent", (room_id,)).fetchall()
    peers = [row["agent"] for row in rows if row["agent"] != agent]
    if len(peers) != 1:
        raise RuntimeError(f"expected exactly one peer for {agent}, found {peers}")
    return peers[0]


def run_once(*, root: Path, task_id: str, wezterm_exe: Path) -> str:
    conn = connect_db(root)
    try:
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        if room is None:
            return "missing_room"
        if room["status"] in TERMINAL_STATUSES:
            return "terminal"
        state = conn.execute("SELECT * FROM tui_relay_state WHERE room_id=?", (task_id,)).fetchone()
        if state and int(state["paused"] or 0):
            return "paused"
        try:
            imported = import_outbox_messages(conn, root=root, room_id=task_id)
        except OutboxError as exc:
            _pause_relay(conn, task_id, f"malformed_outbox:{exc.reason}")
            return "malformed_outbox"
        if imported:
            room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
        if room["status"] in TERMINAL_STATUSES:
            return "terminal"
        if room["status"] == "blocked":
            return "blocked"
        if room["status"] not in ("waiting", "running"):
            return "ignored"
        turn = room["turn"]
        if not turn:
            return "no_turn"
        last_message_id = int(room["last_message_id"])
        if state and state["last_triggered_turn"] == turn and int(state["last_triggered_message_id"]) == last_message_id:
            return "idle"
        pane = conn.execute(
            "SELECT pane_id FROM panes WHERE room_id=? AND pane_role=?",
            (task_id, turn),
        ).fetchone()
        if pane is None or pane["pane_id"] is None:
            _pause_relay(conn, task_id, f"pane_id_missing:{turn}")
            return "missing_pane"
        if not _pane_alive(wezterm_exe, int(pane["pane_id"])):
            _pause_relay(conn, task_id, f"panes_lost:{turn}")
            return "panes_lost"
        first_turn = int(room["round"]) == 0
        peer = _peer_for_agent(conn, task_id, turn)
        text = (
            first_turn_text(agent=turn, task_id=task_id, root=root)
            if first_turn
            else doorbell_text(agent=turn, peer=peer, task_id=task_id, root=root)
        )
        if not send_doorbell(wezterm_exe=wezterm_exe, pane_id=int(pane["pane_id"]), text=text):
            _pause_relay(conn, task_id, f"doorbell_failed:{turn}")
            return "send_failed"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tui_relay_state SET last_triggered_agent=?, last_triggered_turn=?, "
            "last_triggered_message_id=? WHERE room_id=?",
            (turn, turn, last_message_id, task_id),
        )
        conn.execute("COMMIT")
        return "triggered"
    finally:
        conn.close()


def run_watcher_loop(
    *,
    root: Path,
    task_id: str,
    wezterm_exe: Path,
    poll_interval_s: float = 2.0,
    max_iters: Optional[int] = None,
) -> str:
    conn = connect_db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tui_relay_state SET watcher_pid=?, watcher_started_at=?, watcher_host=? WHERE room_id=?",
            (os.getpid(), utc_now(), socket.gethostname(), task_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    iters = 0
    while True:
        result = run_once(root=root, task_id=task_id, wezterm_exe=wezterm_exe)
        if result in {"terminal", "missing_room"}:
            return result
        iters += 1
        if max_iters is not None and iters >= max_iters:
            return "max_iters"
        time.sleep(poll_interval_s)
