import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
MAILBOX_EVAL = SKILL_ROOT / "scripts" / "mailbox_eval.py"
SCENARIOS = SKILL_ROOT / "eval" / "scenarios"


def test_eval_scenarios_are_hard_and_structured():
    files = sorted(SCENARIOS.glob("*.json"))
    assert len(files) >= 5
    categories = set()
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"].startswith("AM-")
        assert data["difficulty"] == "hard"
        assert data["goal"]
        assert data["context"]
        assert data["workspace_files"]
        assert data["success"]["terminal_status"] == "final"
        categories.add(data["category"])
    assert {"context-bootstrap", "blocked-resume", "idempotency", "recovery", "context-overload"}.issubset(categories)


def test_mailbox_eval_refuses_real_without_env():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-01", "--run-real"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 2
    assert "AGENT_MAILBOX_RUN_REAL_SMOKE=1" in rv.stderr


def test_mailbox_eval_initializes_scenario_without_real_agents():
    rv = subprocess.run(
        [sys.executable, str(MAILBOX_EVAL), "--scenario", "AM-01"],
        capture_output=True,
        text=True,
    )
    assert rv.returncode == 0, rv.stderr
    result = json.loads(rv.stdout)
    assert result["scenario_id"] == "AM-01"
    assert result["status"] == "defined"
    assert result["task_id"]
