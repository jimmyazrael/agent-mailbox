import re
from pathlib import Path


SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"


def test_skill_md_documents_quick_start():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "mailbox.py start" in text
    assert "--prefix" in text
    assert "--goal" in text
    assert "--project-cwd" in text


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
