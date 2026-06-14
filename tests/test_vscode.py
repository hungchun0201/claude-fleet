"""VS Code integrated-terminal detection (shell pid resolution)."""
from __future__ import annotations

import pytest

from core import vscode

HELPER = ("/Applications/Visual Studio Code 2.app/Contents/Frameworks/"
          "Code Helper.app/Contents/MacOS/Code Helper --type=utility")
MAIN = "/Applications/Visual Studio Code 2.app/Contents/MacOS/Code"


@pytest.mark.unit
def test_detect_direct_zsh_shell():
    # claude -> zsh -> Code Helper(Frameworks) -> Code main -> launchd
    info = {
        56834: (56543, "claude"),
        56543: (920, "/bin/zsh -il"),
        920: (604, HELPER),
        604: (1, MAIN),
        1: (0, "/sbin/launchd"),
    }
    out = vscode.detect(56834, info)
    assert out == {"shell_pid": 56543, "app": "Visual Studio Code 2"}


@pytest.mark.unit
def test_detect_claude_directly_under_host():
    # No separate shell: the shell pid is claude's own pid.
    info = {
        100: (920, "claude"),
        920: (604, HELPER),
        604: (1, MAIN),
    }
    assert vscode.detect(100, info)["shell_pid"] == 100


@pytest.mark.unit
def test_detect_non_vscode_returns_none():
    # A plain Terminal.app session has no Frameworks pty-host ancestor.
    info = {
        300: (290, "claude"),
        290: (1, "login -fp user"),
        1: (0, "/sbin/launchd"),
    }
    assert vscode.detect(300, info) is None


@pytest.mark.unit
def test_app_name_extraction():
    assert vscode._app_name("/Applications/Cursor.app/Contents/Frameworks/x") == "Cursor"
    assert vscode._app_name("/x/VSCodium.app/Contents/MacOS/Electron") == "VSCodium"
    assert vscode._app_name("/usr/bin/zsh") is None
