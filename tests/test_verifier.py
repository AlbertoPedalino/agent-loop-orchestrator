"""Tests for policy-aware verification command execution."""

from pathlib import Path
import subprocess

import pytest

from agent.policies import PolicyError, validate_commands_allowed
from agent.verifier import run_verification_commands, verification_passed


def test_blocked_verification_command_raises(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        run_verification_commands(tmp_path, ["git push origin main"], ["git push"])


def test_allowed_and_failing_commands_are_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results = iter([
        subprocess.CompletedProcess(["pytest"], 0, stdout="pass", stderr=""),
        subprocess.CompletedProcess(["python"], 1, stdout="", stderr="fail"),
    ])
    monkeypatch.setattr("agent.verifier.subprocess.run", lambda *args, **kwargs: next(results))

    output = run_verification_commands(tmp_path, ["pytest -q", "python -m compileall agent"])

    assert "exit code: 0" in output["pytest -q"]
    assert "exit code: 1" in output["python -m compileall agent"]
    assert not verification_passed(output)
    validate_commands_allowed(["pytest -q"], ["git push"])
