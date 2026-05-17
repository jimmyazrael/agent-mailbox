from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_v1_relay_module_removed():
    assert not (ROOT / "scripts" / "tui_relay.py").exists()


def test_cli_does_not_dispatch_by_relay_version_env():
    text = _read("scripts/mailbox.py")
    assert "AGENT_MAILBOX_RELAY_VERSION" not in text
    assert "from tui_relay import" not in text


def test_no_aggressive_reminder_text_remains():
    forbidden = [
        "previous " + "trigger may not have landed",
        "ignore this duplicate " + "reminder",
        "AGENT_MAILBOX_" + "RELAY_VERSION",
    ]
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for base in ("scripts", "tests", "eval", "SKILL.md", "README.md")
        for path in ((ROOT / base).rglob("*") if (ROOT / base).is_dir() else [ROOT / base])
        if path.is_file() and path.suffix in {".py", ".md", ".json"}
        and path.name != "test_v2_cutover.py"
    )
    for phrase in forbidden:
        assert phrase not in combined
