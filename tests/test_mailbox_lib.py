import os
import subprocess
import sys
import time
from pathlib import Path

from mailbox_lib import default_root, generate_task_id, path_eq, peer_for, pid_exists


def test_default_root_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("AGENT_MAILBOX_ROOT", raising=False)
    assert default_root() == Path.home() / ".agent-mailbox"


def test_default_root_honors_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MAILBOX_ROOT", str(tmp_path))
    assert default_root() == tmp_path


def test_path_eq_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr("mailbox_lib.IS_WINDOWS", True)
    assert path_eq(Path("F:/Programs/X"), Path("f:\\programs\\x"))


def test_path_eq_case_sensitive_on_posix(monkeypatch):
    monkeypatch.setattr("mailbox_lib.IS_WINDOWS", False)
    assert not path_eq(Path("/tmp/Foo"), Path("/tmp/foo"))


def test_generate_task_id_kebab_with_timestamp():
    task_id = generate_task_id("spc", "Phase 2 Review")
    assert task_id.startswith("spc-")
    assert "phase-2-review" in task_id
    parts = task_id.rsplit("-", 2)
    assert len(parts[-1]) == 4 and parts[-1].isdigit()
    assert len(parts[-2]) == 8 and parts[-2].isdigit()


def test_peer_for_two_participants():
    assert peer_for(["claude", "codex"], "claude") == "codex"


def test_pid_exists_edges_and_current_process():
    assert not pid_exists(None)
    assert not pid_exists(0)
    assert not pid_exists(-1)
    assert pid_exists(os.getpid())


def test_pid_exists_returns_false_for_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    for _ in range(10):
        if not pid_exists(proc.pid):
            break
        time.sleep(0.05)
    assert not pid_exists(proc.pid)
