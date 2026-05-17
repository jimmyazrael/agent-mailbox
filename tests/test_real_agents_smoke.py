import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pane_control import build_send_text_argv, build_split_argv
from tui_launcher import find_wezterm, validate_workspace_startup


def _get_text(wezterm_exe: Path, pane_id: int) -> str:
    rv = subprocess.run(
        [
            str(wezterm_exe),
            "cli",
            "--prefer-mux",
            "get-text",
            "--pane-id",
            str(pane_id),
            "--start-line",
            "-80",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    return rv.stdout or ""


def _accept_trust_prompt(wezterm_exe: Path, pane_id: int) -> None:
    deadline = time.time() + 45
    while time.time() < deadline:
        text = _get_text(wezterm_exe, pane_id).lower()
        if "do you trust" in text or "yes, i trust" in text or "yes, continue" in text:
            subprocess.run(
                build_send_text_argv(
                    wezterm_exe=wezterm_exe,
                    pane_id=pane_id,
                    text="1\r",
                    no_paste=True,
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            time.sleep(3)
            # Some TUIs repaint or ask twice. Send once more only if still on a
            # trust prompt after the first selection.
            text = _get_text(wezterm_exe, pane_id).lower()
            if "do you trust" in text or "yes, i trust" in text or "yes, continue" in text:
                subprocess.run(
                    build_send_text_argv(
                        wezterm_exe=wezterm_exe,
                        pane_id=pane_id,
                        text="1\r",
                        no_paste=True,
                    ),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
            return
        time.sleep(1)
    pytest.fail(f"trust prompt did not appear for pane {pane_id}")


def _approve_if_prompted(wezterm_exe: Path, pane_id: int) -> None:
    text = _get_text(wezterm_exe, pane_id).lower()
    if "this command requires approval" in text or "do you want to proceed" in text:
        subprocess.run(
            build_send_text_argv(
                wezterm_exe=wezterm_exe,
                pane_id=pane_id,
                text="1\r",
                no_paste=True,
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )


def _mailbox(*args, timeout=120):
    skill_root = Path(__file__).resolve().parent.parent
    mailbox_py = skill_root / "scripts" / "mailbox.py"
    return subprocess.run(
        [sys.executable, str(mailbox_py), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.real_agents
def test_real_agents_one_round_smoke(tmp_path):
    """Opt-in smoke for U1/U2.

    This intentionally launches real Claude/Codex TUIs through WezTerm. The
    conftest marker skips it unless AGENT_MAILBOX_RUN_REAL_SMOKE=1.
    """
    root = tmp_path / "mb"
    init_rv = _mailbox(
        "init",
        "--root",
        str(root),
        "--prefix",
        "smoke",
        "--label",
        "u1u2",
        "--goal",
        "Claude says hello. Codex replies final. Keep responses one sentence.",
        "--project-cwd",
        str(tmp_path),
        "--first-turn",
        "claude",
        "--format",
        "json",
    )
    assert init_rv.returncode == 0, init_rv.stderr or init_rv.stdout
    task_id = json.loads(init_rv.stdout)["data"]["task_id"]

    launch_rv = _mailbox("launch-tui", "--root", str(root), "--task-id", task_id, "--format", "json")
    assert launch_rv.returncode == 0, launch_rv.stderr or launch_rv.stdout
    launch = json.loads(launch_rv.stdout)["data"]

    wez = find_wezterm()
    startup = validate_workspace_startup(
        wezterm_exe=wez,
        workspace=launch["workspace"],
        claude_pane_id=int(launch["claude_pane_id"]),
        codex_pane_id=int(launch["codex_pane_id"]),
    )
    assert startup["visible"] is True
    assert startup["agents"]["claude"]["state"] in {"ready", "trust_prompt"}
    assert startup["agents"]["codex"]["state"] in {"ready", "trust_prompt"}

    # Real TUIs may stop at workspace-trust prompts for pytest temp dirs. Pick
    # option 1 in both panes before starting the relay so the first trigger lands.
    for pane_key in ("claude_pane_id", "codex_pane_id"):
        _accept_trust_prompt(wez, int(launch[pane_key]))

    startup = validate_workspace_startup(
        wezterm_exe=wez,
        workspace=launch["workspace"],
        claude_pane_id=int(launch["claude_pane_id"]),
        codex_pane_id=int(launch["codex_pane_id"]),
    )
    assert startup["ready"] is True, startup

    skill_root = Path(__file__).resolve().parent.parent
    relay_cmd = [
        "cmd",
        "/c",
        str(skill_root / "scripts" / "launch_relay_pane.cmd"),
        task_id,
        str(root),
        str(tmp_path),
        str(skill_root / "scripts" / "mailbox.py"),
        "30",
    ]
    relay_rv = subprocess.run(
        build_split_argv(
            wezterm_exe=wez,
            source_pane_id=int(launch["codex_pane_id"]),
            direction="bottom",
            percent=25,
            cwd=tmp_path,
            cmd=relay_cmd,
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert relay_rv.returncode == 0, relay_rv.stderr or relay_rv.stdout

    # Rebind the relay pane if WezTerm reported an id; not required for the
    # smoke assertions, but it keeps status output accurate.
    if relay_rv.stdout.strip().isdigit():
        _mailbox(
            "repair",
            "--root",
            str(root),
            "--task-id",
            task_id,
            "--rebind-pane",
            "--agent",
            "relay",
            "--pane-id",
            relay_rv.stdout.strip(),
        )

    from agent_chat import connect_db, room_state_get

    deadline = time.time() + 300
    last_status = None
    last_message_id = 0
    while time.time() < deadline:
        conn = connect_db(root)
        try:
            room = conn.execute("SELECT * FROM rooms WHERE id=?", (task_id,)).fetchone()
            last_status = room["status"]
            last_message_id = room["last_message_id"]
            if last_status in {"final", "error", "stopped"}:
                sessions = {
                    row["agent"]: dict(row)
                    for row in conn.execute("SELECT * FROM agent_sessions WHERE room_id=?", (task_id,)).fetchall()
                }
                usage = room_state_get(conn, task_id, "usage", default={}) or {}
                break
        finally:
            conn.close()
        for pane_key in ("claude_pane_id", "codex_pane_id"):
            _approve_if_prompted(wez, int(launch[pane_key]))
        time.sleep(5)
    else:
        pytest.fail(f"real-agent smoke timed out; status={last_status!r}, last_message_id={last_message_id}")

    assert last_status == "final"
    assert last_message_id >= 2
    assert sessions["claude"]["session_id"]
    assert sessions["codex"]["discovery_status"] == "discovered"
    assert float(usage.get("known_cost_usd", 0.0)) <= 0.10
    _mailbox("stop", "--root", str(root), "--task-id", task_id, "--close-panes", "--yes")
