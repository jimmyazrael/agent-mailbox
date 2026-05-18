#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pane_control import build_get_text_argv, build_kill_pane_argv, build_send_text_argv, build_spawn_argv
from tui_launcher import find_wezterm


def _run(argv: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _send(wezterm_exe: Path, pane_id: int, text: str) -> subprocess.CompletedProcess[str]:
    return _run(build_send_text_argv(wezterm_exe=wezterm_exe, pane_id=pane_id, text=text, no_paste=True))


def run_smoke(*, workspace: str = "agent-mailbox-prompt-smoke", keep: bool = False) -> dict[str, object]:
    wez = find_wezterm()
    with tempfile.TemporaryDirectory(prefix="agent_mailbox_prompt_smoke_") as tmp:
        cwd = Path(tmp)
        probe = cwd / "probe.txt"
        cmd = ["cmd", "/k", "prompt $G"]
        spawn = _run(build_spawn_argv(wezterm_exe=wez, workspace=workspace, cwd=cwd, cmd=cmd))
        if spawn.returncode != 0:
            return {"ok": False, "error": spawn.stderr or spawn.stdout}
        pane_text = spawn.stdout.strip()
        pane_id = int(pane_text) if pane_text.isdigit() else None
        if pane_id is None:
            return {"ok": False, "error": f"could not parse pane id from: {pane_text!r}"}
        try:
            one_call = _send(wez, pane_id, f"echo ONE-CALL> {probe}\r\n")
            readback = _run(build_get_text_argv(wezterm_exe=wez, pane_id=pane_id), timeout=10)
            exists = probe.is_file() and probe.read_text(encoding="utf-8", errors="replace").strip() == "ONE-CALL"
            return {
                "ok": exists and one_call.returncode == 0,
                "workspace": workspace,
                "pane_id": pane_id,
                "one_call_rc": one_call.returncode,
                "probe": str(probe),
                "probe_exists": probe.is_file(),
                "probe_text": probe.read_text(encoding="utf-8", errors="replace").strip() if probe.is_file() else None,
                "pane_tail": (readback.stdout or "")[-500:],
            }
        finally:
            if not keep and pane_id is not None:
                _run(build_kill_pane_argv(wezterm_exe=wez, pane_id=pane_id), timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test WezTerm prompt delivery with one send-text call.")
    parser.add_argument("--workspace", default="agent-mailbox-prompt-smoke")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    result = run_smoke(workspace=args.workspace, keep=args.keep)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
