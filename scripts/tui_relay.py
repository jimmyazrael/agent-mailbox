from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from agent_chat import connect_db
from mailbox_lib import TERMINAL_STATUSES, utc_now
from pane_control import build_list_argv, build_send_text_argv
from tui_launcher import lookup_pane, parse_wezterm_list


def trigger_text(*, agent: str, peer: str, task_id: str, root: Path, first_turn: bool = False) -> str:
    lead = (
        f"You have the first turn on agent-mailbox task {task_id}."
        if first_turn
        else f"{peer.capitalize()} has replied on agent-mailbox task {task_id}."
    )
    mailbox_py = Path(__file__).resolve().parent / "mailbox.py"
    return (
        f"{lead} "
        f"Read the latest mailbox state with: python \"{mailbox_py}\" show --root \"{root}\" --task-id \"{task_id}\" --tail 1 --body --format json. "
        f"Then post via: python \"{mailbox_py}\" post --root \"{root}\" --task-id \"{task_id}\" --from {agent} --to {peer} --status continue --summary \"...\" --body \"...\". "
        f"If you already responded to that message, do nothing.\r"
    )


def send_trigger(*, wezterm_exe: Path, pane_id: int, agent: str, peer: str, task_id: str, root: Path, first_turn: bool = False) -> bool:
    text = trigger_text(agent=agent, peer=peer, task_id=task_id, root=root, first_turn=first_turn)
    # Real Claude/Codex TUIs may accept the text but ignore a trailing CR in the
    # same send-text call. Send Enter separately; this validated U1 on Windows.
    rv_text = subprocess.run(
        build_send_text_argv(
            wezterm_exe=wezterm_exe,
            pane_id=pane_id,
            text=text.rstrip("\r"),
            no_paste=True,
        ),
        capture_output=True,
        text=True,
    )
    if rv_text.returncode != 0:
        return False
    rv_enter = subprocess.run(
        build_send_text_argv(
            wezterm_exe=wezterm_exe,
            pane_id=pane_id,
            text="\r",
            no_paste=True,
        ),
        capture_output=True,
        text=True,
    )
    return rv_enter.returncode == 0


def _pane_alive(wezterm_exe: Path, pane_id: int) -> bool:
    rv = subprocess.run(
        build_list_argv(wezterm_exe=wezterm_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if rv.returncode != 0:
        return False
    try:
        panes = parse_wezterm_list(rv.stdout)
    except (ValueError, TypeError):
        return False
    return bool(lookup_pane(panes, pane_id=pane_id))


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
        if room["status"] == "blocked":
            return "blocked"
        if room["status"] not in ("waiting", "running"):
            return "ignored"
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
                (f"pane_id_missing:{turn}", task_id),
            )
            conn.execute("COMMIT")
            return "missing_pane"
        if not _pane_alive(wezterm_exe, int(pane["pane_id"])):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE tui_relay_state SET paused=1, pause_reason=? WHERE room_id=?",
                (f"panes_lost:{turn}", task_id),
            )
            conn.execute("COMMIT")
            return "panes_lost"
        peer = _peer_for_agent(conn, task_id, turn)
        if not send_trigger(
            wezterm_exe=wezterm_exe,
            pane_id=int(pane["pane_id"]),
            agent=turn,
            peer=peer,
            task_id=task_id,
            root=root,
            first_turn=last_message_id == 0,
        ):
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
