import json
from pathlib import Path

from session_logs import (
    claude_completed_text,
    codex_completed_text,
    find_claude_session_log,
    find_codex_session_log,
    latest_completed_turn,
)


def test_codex_completed_text_uses_final_answer_only():
    commentary = {
        "type": "response_item",
        "payload": {"role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "working"}]},
    }
    final = {
        "type": "response_item",
        "payload": {"role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]},
    }
    assert codex_completed_text(commentary) is None
    assert codex_completed_text(final) == "done"


def test_claude_completed_text_uses_end_turn_only():
    tool_use = {
        "type": "assistant",
        "message": {"role": "assistant", "stop_reason": "tool_use", "content": [{"type": "text", "text": "mid"}]},
    }
    done = {
        "type": "assistant",
        "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "final"}]},
    }
    assert claude_completed_text(tool_use) is None
    assert claude_completed_text(done) == "final"


def test_latest_completed_turn_ignores_partial_jsonl_line(tmp_path):
    path = tmp_path / "rollout-abc.jsonl"
    rows = [
        {"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "first"}]}},
        {"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "second"}]}},
    ]
    path.write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]), encoding="utf-8")
    latest = latest_completed_turn(path, agent="codex")
    assert latest["text"] == "first"


def test_find_codex_session_log_by_id(tmp_path):
    folder = tmp_path / "2026" / "05" / "17"
    folder.mkdir(parents=True)
    path = folder / "rollout-2026-05-17T00-00-00-session-1.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert find_codex_session_log(session_id="session-1", sessions_root=tmp_path) == path


def test_find_claude_session_log_by_project_and_id(tmp_path):
    project = tmp_path / "f--Programs-LocalAgentConcept"
    project.mkdir()
    path = project / "claude-1.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert find_claude_session_log(session_id="claude-1", project_cwd=Path("f:/Programs/LocalAgentConcept"), projects_root=tmp_path) == path


def test_find_claude_session_log_preserves_double_hyphen_drive_separator(tmp_path):
    project = tmp_path / "C--Users-Jimmy-AppData-Local-Temp-project"
    project.mkdir()
    path = project / "claude-2.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert find_claude_session_log(session_id="claude-2", project_cwd=Path("C:/Users/Jimmy/AppData/Local/Temp/project"), projects_root=tmp_path) == path
