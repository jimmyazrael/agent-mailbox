import json
from datetime import datetime, timedelta
from pathlib import Path

from codex_session_discovery import find_codex_session_id


def _make_rollout(root: Path, day_offset: int, *, session_id: str, cwd: str, marker: str | None = None, originator: str = "codex-tui"):
    stamp = datetime.now() - timedelta(days=day_offset)
    folder = root / stamp.strftime("%Y") / stamp.strftime("%m") / stamp.strftime("%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{stamp.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    lines = [
        {"timestamp": stamp.isoformat(), "type": "session_meta", "payload": {"id": session_id, "cwd": cwd, "originator": originator}},
    ]
    if marker:
        lines.append(
            {
                "timestamp": stamp.isoformat(),
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": marker}]},
            }
        )
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def test_find_codex_session_id_matches_marker_and_cwd(tmp_path):
    _make_rollout(tmp_path, 0, session_id="wrong", cwd="F:/wrong", marker="AGENT_MAILBOX_TASK_ID=t1")
    _make_rollout(tmp_path, 0, session_id="right", cwd="F:/proj", marker="AGENT_MAILBOX_TASK_ID=t1")
    result = find_codex_session_id(task_id="t1", project_cwd=Path("F:/proj"), sessions_root=tmp_path)
    assert result["status"] == "discovered"
    assert result["session_id"] == "right"


def test_find_codex_session_id_accepts_colon_marker(tmp_path):
    _make_rollout(tmp_path, 0, session_id="right", cwd="F:/proj", marker="AGENT_MAILBOX_TASK_ID: t1")
    result = find_codex_session_id(task_id="t1", project_cwd=Path("F:/proj"), sessions_root=tmp_path)
    assert result["status"] == "discovered"
    assert result["session_id"] == "right"


def test_find_codex_session_id_returns_failed(tmp_path):
    _make_rollout(tmp_path, 0, session_id="none", cwd="F:/proj")
    result = find_codex_session_id(task_id="t1", project_cwd=Path("F:/proj"), sessions_root=tmp_path)
    assert result["status"] == "failed"
    assert result["session_id"] is None


def test_find_codex_session_id_skips_old_files(tmp_path):
    _make_rollout(tmp_path, 30, session_id="old", cwd="F:/proj", marker="AGENT_MAILBOX_TASK_ID=t1")
    result = find_codex_session_id(task_id="t1", project_cwd=Path("F:/proj"), sessions_root=tmp_path, max_age_days=7)
    assert result["status"] == "failed"


def test_find_codex_session_id_ignores_tool_output_marker(tmp_path):
    stamp = datetime.now()
    folder = tmp_path / stamp.strftime("%Y") / stamp.strftime("%m") / stamp.strftime("%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "rollout-x.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "bad", "cwd": "F:/proj", "originator": "codex-tui"}},
        {"type": "response_item", "payload": {"role": "assistant", "content": [{"text": "AGENT_MAILBOX_TASK_ID=t1"}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    result = find_codex_session_id(task_id="t1", project_cwd=Path("F:/proj"), sessions_root=tmp_path)
    assert result["status"] == "failed"
