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

MISSING_OUTBOX_GRACE_S = 180.0


def doorbell_text(*, agent: str, peer: str, task_id: str, root: Path) -> str:
    write_path = next_outbox_path(root, task_id, agent)
    return (
        f"{peer.capitalize()} has replied on agent-mailbox task {task_id}.\n\n"
        "Read the latest reply in the chat monitor pane.\n\n"
        f"To respond, write your reply to: {write_path}\n\n"
        "Required frontmatter (exact field names):\n"
        f"  from: {agent}\n"
        f"  to: {peer}\n"
        "  status: <continue | blocked | final | error>\n"
        "  summary: <one-line summary>\n\n"
        "The chat monitor may show messages addressed to your peer.\n"
        f"Only act on messages where `to: {agent}` or where the user has injected to you or to broadcast.\n"
        f"If a message is `to: {peer}`, do not respond to it.\n\n"
        "End the file with the literal sentinel on its own line: <!-- AGENT-MAILBOX:DONE -->\n\n"
        "Then stop.\r"
    )


def first_turn_text(*, agent: str, peer: str, task_id: str, root: Path) -> str:
    write_path = next_outbox_path(root, task_id, agent)
    return (
        f"You have the first turn on agent-mailbox task {task_id}.\n\n"
        "Read the bootstrap context in the chat monitor pane.\n\n"
        f"To respond, write your reply to: {write_path}\n\n"
        "Required frontmatter (exact field names):\n"
        f"  from: {agent}\n"
        f"  to: {peer}\n"
        "  status: <continue | blocked | final | error>\n"
        "  summary: <one-line summary>\n\n"
        "The chat monitor may show messages addressed to your peer.\n"
        f"Only act on messages where `to: {agent}` or where the user has injected to you or to broadcast.\n"
        f"If a message is `to: {peer}`, do not respond to it.\n\n"
        "End the file with the literal sentinel on its own line: <!-- AGENT-MAILBOX:DONE -->\n\n"
        "Then stop.\r"
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


def _session_safety_state(conn, room_id: str) -> dict:
    from agent_chat import room_state_get

    value = room_state_get(conn, room_id, "session_log_safety", default={})
    return value if isinstance(value, dict) else {}


def _set_session_safety_state(conn, room_id: str, state: dict) -> None:
    from agent_chat import room_state_set

    room_state_set(conn, room_id, "session_log_safety", state)


def _mark_doorbell_sent(conn, *, room_id: str, agent: str, now: float) -> None:
    state = _session_safety_state(conn, room_id)
    agent_state = state.get(agent) if isinstance(state.get(agent), dict) else {}
    # A single logical doorbell turn can contain multiple session-log end_turn
    # records before the agent writes its outbox. Correlate safety to doorbells,
    # not to the first completion observed inside that turn.
    for key in ("completion_hash", "first_seen_monotonic", "session_path", "session_timestamp"):
        agent_state.pop(key, None)
    agent_state["last_doorbell_monotonic"] = now
    state[agent] = agent_state
    _set_session_safety_state(conn, room_id, state)


def _mark_outbox_imported(conn, *, room_id: str, agent: str, message_id: int, completion_hash: Optional[str]) -> None:
    state = _session_safety_state(conn, room_id)
    agent_state = state.get(agent) if isinstance(state.get(agent), dict) else {}
    agent_state["last_outbox_message_id"] = message_id
    if completion_hash is not None:
        agent_state["handled_completion_hash"] = completion_hash
    for key in ("last_doorbell_monotonic", "completion_hash", "first_seen_monotonic", "session_path", "session_timestamp"):
        agent_state.pop(key, None)
    state[agent] = agent_state
    _set_session_safety_state(conn, room_id, state)


def _check_missing_outbox_after_completion(conn, *, root: Path, task_id: str, agent: str, now: float) -> Optional[str]:
    from session_logs import find_session_log, latest_completed_turn

    room = conn.execute("SELECT project_cwd FROM rooms WHERE id=?", (task_id,)).fetchone()
    session = conn.execute(
        "SELECT session_id FROM agent_sessions WHERE room_id=? AND agent=?",
        (task_id, agent),
    ).fetchone()
    if room is None or session is None or not session["session_id"]:
        return None
    log_path = find_session_log(agent=agent, session_id=session["session_id"], project_cwd=Path(room["project_cwd"]))
    if log_path is None:
        return None
    latest = latest_completed_turn(log_path, agent=agent)
    if latest is None:
        return None
    state = _session_safety_state(conn, task_id)
    agent_state = state.get(agent) if isinstance(state.get(agent), dict) else {}
    latest_hash = latest["hash"]
    if agent_state.get("handled_completion_hash") == latest_hash:
        return None
    last_doorbell = agent_state.get("last_doorbell_monotonic")
    if last_doorbell is None:
        return None
    if now - float(last_doorbell) < MISSING_OUTBOX_GRACE_S:
        if agent_state.get("completion_hash") != latest_hash:
            agent_state["completion_hash"] = latest_hash
            agent_state["session_path"] = latest["path"]
            agent_state["session_timestamp"] = latest.get("timestamp")
            state[agent] = agent_state
            _set_session_safety_state(conn, task_id, state)
        return None
    if agent_state.get("completion_hash") != latest_hash:
        agent_state["completion_hash"] = latest_hash
        agent_state["session_path"] = latest["path"]
        agent_state["session_timestamp"] = latest.get("timestamp")
        state[agent] = agent_state
        _set_session_safety_state(conn, task_id, state)
    return f"missing_outbox_after_turn:{agent}"


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
            from session_logs import find_session_log, latest_completed_turn

            room_for_logs = conn.execute("SELECT project_cwd FROM rooms WHERE id=?", (task_id,)).fetchone()
            for item in imported:
                msg = item["message"]
                completion_hash = None
                session = conn.execute(
                    "SELECT session_id FROM agent_sessions WHERE room_id=? AND agent=?",
                    (task_id, msg.from_agent),
                ).fetchone()
                if room_for_logs is not None and session is not None and session["session_id"]:
                    log_path = find_session_log(
                        agent=msg.from_agent,
                        session_id=session["session_id"],
                        project_cwd=Path(room_for_logs["project_cwd"]),
                    )
                    latest = latest_completed_turn(log_path, agent=msg.from_agent) if log_path else None
                    if latest is not None:
                        completion_hash = latest["hash"]
                _mark_outbox_imported(
                    conn,
                    room_id=task_id,
                    agent=msg.from_agent,
                    message_id=int(item["message_id"]),
                    completion_hash=completion_hash,
                )
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
        if state and state["last_doorbell_turn"] == turn and int(state["last_doorbell_message_id"]) == last_message_id:
            reason = _check_missing_outbox_after_completion(conn, root=root, task_id=task_id, agent=turn, now=time.monotonic())
            if reason:
                _pause_relay(conn, task_id, reason)
                return "missing_outbox_after_turn"
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
            first_turn_text(agent=turn, peer=peer, task_id=task_id, root=root)
            if first_turn
            else doorbell_text(agent=turn, peer=peer, task_id=task_id, root=root)
        )
        if not send_doorbell(wezterm_exe=wezterm_exe, pane_id=int(pane["pane_id"]), text=text):
            _pause_relay(conn, task_id, f"doorbell_failed:{turn}")
            return "send_failed"
        now = time.monotonic()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tui_relay_state SET last_doorbell_agent=?, last_doorbell_turn=?, "
            "last_doorbell_message_id=? WHERE room_id=?",
            (turn, turn, last_message_id, task_id),
        )
        _mark_doorbell_sent(conn, room_id=task_id, agent=turn, now=now)
        conn.execute("COMMIT")
        return "doorbell_sent"
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
    result = "unknown"
    try:
        iters = 0
        while True:
            result = run_once(root=root, task_id=task_id, wezterm_exe=wezterm_exe)
            if result in {"terminal", "missing_room", "paused", "malformed_outbox", "missing_outbox_after_turn", "missing_pane", "panes_lost", "send_failed"}:
                return result
            iters += 1
            if max_iters is not None and iters >= max_iters:
                result = "max_iters"
                return result
            time.sleep(poll_interval_s)
    finally:
        conn = connect_db(root)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE tui_relay_state SET watcher_last_result=?, watcher_finished_at=? WHERE room_id=?",
                (result, utc_now(), task_id),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
