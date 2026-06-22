"""Tests for the Claude CLI boundary; subprocess is always mocked."""

from pathlib import Path
import subprocess

import pytest

from agent.claude_runner import ClaudeRunnerError, _build_claude_command, run_claude_prompt


def test_build_command_includes_only_supplied_flags() -> None:
    command = _build_claude_command(
        "plan",
        max_turns=4,
        output_format=None,
        max_budget_usd=1.5,
    )

    assert command == ["claude", "-p", "plan", "--max-turns", "4", "--max-budget-usd", "1.5"]


def test_successful_prompt_returns_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="planned", stderr="")

    monkeypatch.setattr("agent.claude_runner.subprocess.run", fake_run)

    assert run_claude_prompt("plan", tmp_path, max_turns=3, max_budget_usd=2.0, phase="planner") == "planned"
    assert captured["command"] == [
        "claude",
        "-p",
        "plan",
        "--max-turns",
        "3",
        "--output-format",
        "text",
        "--max-budget-usd",
        "2.0",
    ]
    assert captured["kwargs"] == {
        "cwd": tmp_path.resolve(),
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": 600,
    }


def test_missing_or_non_directory_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_claude_prompt("plan", tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        run_claude_prompt("plan", file_path)


def test_nonzero_result_raises_with_safe_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent.claude_runner.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 7, stdout="out", stderr="err"),
    )

    with pytest.raises(ClaudeRunnerError) as error:
        run_claude_prompt("private prompt", tmp_path, phase="fixer")

    message = str(error.value)
    assert "fixer" in message
    assert "return code 7" in message
    assert "<redacted-prompt>" in message
    assert "private prompt" not in message
