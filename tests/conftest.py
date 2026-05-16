import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "real_agents: invokes real Claude/Codex CLIs")
    config.addinivalue_line("markers", "real_wezterm: invokes real WezTerm")


def pytest_collection_modifyitems(config, items):
    skip_real = pytest.mark.skip(reason="set AGENT_MAILBOX_RUN_REAL_SMOKE=1 to run")
    skip_tui = pytest.mark.skip(reason="set AGENT_MAILBOX_RUN_TUI_SMOKE=1 to run")
    real_on = os.environ.get("AGENT_MAILBOX_RUN_REAL_SMOKE") == "1"
    tui_on = os.environ.get("AGENT_MAILBOX_RUN_TUI_SMOKE") == "1"
    for item in items:
        if "real_agents" in item.keywords and not real_on:
            item.add_marker(skip_real)
        if "real_wezterm" in item.keywords and not tui_on:
            item.add_marker(skip_tui)
