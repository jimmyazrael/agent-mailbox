from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from agent_chat import connect_db
from mailbox_lib import TERMINAL_STATUSES, utc_now
from pane_control import build_send_text_argv


def trigger_text(*, agent: str, peer: str, task_id: str) -> str:
    return (
        f"{peer} has replied in agent-mailbox task {task_id}. "
        f"Read the mailbox, respond if needed, and post via mailbox.py. "
        f"If your prior response already covers the latest message, do not post again.\r"
    )


def send_trigger(*, wezterm_exe: Path, pane_id: int, agent: str, peer: str, task_id: str) -> bool:
    rv = subprocess.run(
        build_send_text_argv(
            wezterm_exe=wezterm_exe,
            pane_id=pane_id,
            text=trigger_text(agent=agent, peer=peer, task_id=task_id),
            no_paste=True,
        ),
        capture_output=True,
        text=True,
    )
    return rv.returncode == 0


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
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
                (f"panes_lost:{turn}", task_id),
            )
            conn.execute("COMMIT")
            return "missing_pane"
        peer = _peer_for_agent(conn, task_id, turn)
        if not send_trigger(wezterm_exe=wezterm_exe, pane_id=int(pane["pane_id"]), agent=turn, peer=peer, task_id=task_id):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
                (f"trigger_failed:{turn}", task_id),
            )
            conn.execute("COMMIT")
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
