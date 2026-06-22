"""Tests for deterministic hook guardrails."""

from pathlib import Path

import pytest

from agent.hooks import HookViolationError, post_command_hook, post_phase_hook, pre_command_hook, pre_phase_hook


def test_blocked_pre_command_hook_raises() -> None:
    with pytest.raises(HookViolationError):
        pre_command_hook("GIT PUSH origin main", ["git push"])


def test_allowed_pre_command_hook_and_metadata(tmp_path: Path) -> None:
    pre_command_hook("pytest -q", ["git push"])
    assert post_command_hook("pytest -q", 0, "ok") == {
        "command": "pytest -q",
        "return_code": 0,
        "output_length": 2,
        "status": "passed",
    }
    assert pre_phase_hook("planner", tmp_path, "task")["phase"] == "planner"
    assert post_phase_hook("planner", "output")["output_length"] == "6"
