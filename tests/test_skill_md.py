import re
from pathlib import Path


SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"


def test_skill_md_documents_quick_start():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "mailbox.py start" in text
    assert "--prefix" in text
    assert "--goal" in text
    assert "--project-cwd" in text


def test_skill_md_documents_day_to_day_context_bootstrap():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "Day-To-Day Invocation" in text
    assert "Bootstrap context file template" in text
    assert "Relevant Files:" in text
    assert "Constraints:" in text
    assert "Done Criteria:" in text
    assert "Do not ask the user to manually craft the context" in text


def test_skill_md_documents_statuses_and_recovery():
    text = SKILL_MD.read_text(encoding="utf-8")
    for status in ("continue", "blocked", "final", "error"):
        assert status in text
    assert "mailbox.py resume" in text
    assert re.search(r"crash|recover|restart", text, re.IGNORECASE)


def test_skill_md_has_must_not_list():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "MUST NOT" in text
    assert "agent-chat.sqlite" in text
    assert "codex resume --last" in text
