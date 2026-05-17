from pathlib import Path

from pane_control import build_activate_pane_argv, build_list_argv, build_send_text_argv, build_spawn_argv


WEZ = Path("C:/Program Files/WezTerm/wezterm.exe")


def test_build_spawn_argv_basic():
    argv = build_spawn_argv(wezterm_exe=WEZ, workspace="agent-mailbox-t1", cwd=Path("F:/proj"), cmd=["cmd", "/c", "launch.cmd"])
    assert argv[0].endswith("wezterm.exe")
    assert "--prefer-mux" in argv
    assert "spawn" in argv
    assert "--new-window" in argv
    assert "--workspace" in argv and "agent-mailbox-t1" in argv
    assert "--cwd" in argv and str(Path("F:/proj")) in argv
    assert argv[argv.index("--") + 1 :] == ["cmd", "/c", "launch.cmd"]


def test_build_send_text_argv_no_paste():
    argv = build_send_text_argv(wezterm_exe=WEZ, pane_id=42, text="hello\r")
    assert "send-text" in argv
    assert "--no-paste" in argv
    assert "--pane-id" in argv and "42" in argv
    assert argv[-1] == "hello\r"


def test_build_list_argv_json():
    argv = build_list_argv(wezterm_exe=WEZ)
    assert "list" in argv and "--format" in argv and "json" in argv


def test_build_activate_pane_argv():
    argv = build_activate_pane_argv(wezterm_exe=WEZ, pane_id=42)
    assert "activate-pane" in argv
    assert "--pane-id" in argv and "42" in argv
