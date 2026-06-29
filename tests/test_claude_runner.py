"""Tests for the Claude CLI boundary; the subprocess layer is always mocked."""

from pathlib import Path
import subprocess

import pytest

from agent.claude_runner import ClaudeRunnerError, _build_claude_command, run_claude_prompt


def test_build_command_includes_only_supplied_flags() -> None:
    command = _build_claude_command(
        "plan",
        allowed_tools=["Read", "Grep"],
        disallowed_tools=None,
        permission_mode=None,
        output_format=None,
        max_budget_usd=1.5,
    )

    assert command == [
        "claude",
        "-p",
        "plan",
        "--allowedTools",
        "Read,Grep",
        "--max-budget-usd",
        "1.5",
    ]


def test_build_command_never_passes_max_turns() -> None:
    command = _build_claude_command(
        "plan",
        allowed_tools=None,
        disallowed_tools=None,
        permission_mode=None,
        output_format=None,
        max_budget_usd=None,
    )

    assert "--max-turns" not in command


def test_build_command_stream_json_adds_verbose() -> None:
    command = _build_claude_command(
        "plan",
        allowed_tools=None,
        disallowed_tools=None,
        permission_mode=None,
        output_format="stream-json",
        max_budget_usd=None,
    )

    assert "--output-format" in command
    assert "stream-json" in command
    assert "--verbose" in command


def test_captured_prompt_returns_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="edited", stderr="")

    monkeypatch.setattr("agent.claude_runner.subprocess.run", fake_run)

    output = run_claude_prompt(
        "implement",
        tmp_path,
        allowed_tools=["Edit", "Bash"],
        disallowed_tools=["Bash(git push:*)"],
        permission_mode="acceptEdits",
        max_budget_usd=2.0,
        timeout_seconds=300,
        stream=False,
        phase="implementer",
    )

    assert output == "edited"
    assert captured["command"] == [
        "claude",
        "-p",
        "implement",
        "--allowedTools",
        "Edit,Bash",
        "--disallowedTools",
        "Bash(git push:*)",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "text",
        "--max-budget-usd",
        "2.0",
    ]
    assert captured["kwargs"] == {
        "cwd": tmp_path.resolve(),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
        "timeout": 300,
    }


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


class _FakeStderr:
    def read(self) -> str:
        return ""


class _FakeProcess:
    def __init__(self, lines: list[str], return_code: int = 0) -> None:
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStderr()
        self._return_code = return_code
        self.killed = False

    def wait(self) -> int:
        return self._return_code

    def kill(self) -> None:
        self.killed = True


def test_streaming_prompt_extracts_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [
        '{"type":"system","subtype":"init","model":"claude-test"}\n',
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"Looking at the repo"},'
        '{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}\n',
        '{"type":"result","subtype":"success","result":"FINAL REPORT",'
        '"num_turns":3,"total_cost_usd":0.12}\n',
    ]
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        return _FakeProcess(lines)

    monkeypatch.setattr("agent.claude_runner.subprocess.Popen", fake_popen)

    output = run_claude_prompt("inspect", tmp_path, phase="planner")

    assert output == "FINAL REPORT"
    assert "--output-format" in captured["command"]
    assert "stream-json" in captured["command"]
    assert "--verbose" in captured["command"]


def test_streaming_nonzero_return_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent.claude_runner.subprocess.Popen",
        lambda command, **kwargs: _FakeProcess(["not-json\n"], return_code=2),
    )

    with pytest.raises(ClaudeRunnerError, match="return code 2"):
        run_claude_prompt("inspect", tmp_path, phase="planner")


def test_streaming_budget_limit_keeps_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [
        '{"type":"result","subtype":"error_max_budget","result":"REPORT DONE",'
        '"num_turns":32,"total_cost_usd":2.13}\n',
    ]
    monkeypatch.setattr(
        "agent.claude_runner.subprocess.Popen",
        lambda command, **kwargs: _FakeProcess(lines, return_code=1),
    )

    # Non-zero exit, but a result event was produced -> keep the partial output.
    assert run_claude_prompt("inspect", tmp_path, phase="planner") == "REPORT DONE"


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
        run_claude_prompt("private prompt", tmp_path, stream=False, phase="fixer")

    message = str(error.value)
    assert "fixer" in message
    assert "return code 7" in message
    assert "<redacted-prompt>" in message
    assert "private prompt" not in message
