"""Tests for the Codex CLI boundary; the subprocess layer is always mocked."""

from pathlib import Path
import subprocess
import threading

import pytest

from agent.codex_runner import (
    CodexRunnerError,
    CodexTransientError,
    _build_codex_command,
    _looks_transient,
    _prefix_for_codex_path,
    _resolve_codex_command_prefix,
    _sandbox_for_tools,
    run_codex_prompt,
)


def test_build_codex_command_reads_prompt_from_stdin(tmp_path: Path) -> None:
    output_path = tmp_path / "last.md"

    command = _build_codex_command(
        repo_path=tmp_path,
        output_path=output_path,
        sandbox_mode="read-only",
        stream=True,
    )

    assert command == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--cd",
        str(tmp_path),
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        str(output_path),
        "--json",
        "-",
    ]


def test_build_codex_command_accepts_resolved_cli_prefix(tmp_path: Path) -> None:
    output_path = tmp_path / "last.md"

    command = _build_codex_command(
        repo_path=tmp_path,
        output_path=output_path,
        sandbox_mode="read-only",
        stream=False,
        cli_prefix=["powershell.exe", "-File", "codex.ps1"],
    )

    assert command[:6] == [
        "powershell.exe",
        "-File",
        "codex.ps1",
        "--ask-for-approval",
        "never",
        "exec",
    ]
    assert "--json" not in command
    assert command[-1] == "-"


def test_windows_powershell_codex_shim_is_launched_via_powershell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "codex.ps1"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr("agent.codex_runner.os.name", "nt")
    monkeypatch.setattr(
        "agent.codex_runner.shutil.which",
        lambda name: "powershell.exe" if name == "powershell" else None,
    )

    assert _prefix_for_codex_path(str(script)) == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def test_windowsapps_pwsh_alias_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "codex.ps1"
    script.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        if name in {"pwsh", "pwsh.exe"}:
            return r"C:\Users\alber\AppData\Local\Microsoft\WindowsApps\pwsh.exe"
        if name == "powershell":
            return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        return None

    monkeypatch.setattr("agent.codex_runner.os.name", "nt")
    monkeypatch.setattr("agent.codex_runner.shutil.which", fake_which)

    assert _prefix_for_codex_path(str(script)) == [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def test_windows_cmd_shim_prefers_adjacent_powershell_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd = tmp_path / "codex.cmd"
    cmd.write_text("", encoding="utf-8")
    script = tmp_path / "codex.ps1"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr("agent.codex_runner.os.name", "nt")
    monkeypatch.setattr(
        "agent.codex_runner.shutil.which",
        lambda name: "powershell.exe" if name == "powershell" else None,
    )

    assert _prefix_for_codex_path(str(cmd)) == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def test_windows_resolver_prefers_powershell_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "codex.ps1"
    script.write_text("", encoding="utf-8")
    cmd = tmp_path / "codex.cmd"
    cmd.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        if name == "codex.ps1":
            return str(script)
        if name == "codex.cmd":
            return str(cmd)
        if name == "powershell":
            return "powershell.exe"
        return None

    monkeypatch.setattr("agent.codex_runner.os.name", "nt")
    monkeypatch.setattr("agent.codex_runner.shutil.which", fake_which)

    assert _resolve_codex_command_prefix() == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def test_sandbox_follows_phase_mutability() -> None:
    assert _sandbox_for_tools(["Read", "Grep"]) == "read-only"
    assert _sandbox_for_tools(["Read", "Edit"]) == "workspace-write"
    assert _sandbox_for_tools(["Bash"]) == "workspace-write"


def test_captured_prompt_returns_output_last_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("final answer", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="event log", stderr="")

    monkeypatch.setattr("agent.codex_runner.subprocess.run", fake_run)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    output = run_codex_prompt(
        "implement",
        tmp_path,
        allowed_tools=["Read", "Edit"],
        disallowed_tools=["Bash(git push:*)"],
        permission_mode="acceptEdits",
        timeout_seconds=300,
        stream=False,
        phase="implementer",
    )

    assert output == "final answer"
    assert captured["command"][0] == "codex.cmd"
    assert "--sandbox" in captured["command"]
    assert "workspace-write" in captured["command"]
    assert captured["kwargs"]["cwd"] == tmp_path.resolve()
    assert captured["kwargs"]["input"].startswith("implement")
    assert "Bash(git push:*)" in captured["kwargs"]["input"]
    assert "implement" not in captured["command"]


def test_cli_backend_strips_codex_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["kwargs"] = kwargs
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CODEX_API_KEY", "secret")
    monkeypatch.setattr("agent.codex_runner.subprocess.run", fake_run)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    assert run_codex_prompt("plan", tmp_path, stream=False) == "ok"

    env = captured["kwargs"]["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env


def test_api_backend_injects_codex_api_key_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["kwargs"] = kwargs
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "codex-key")
    monkeypatch.setattr("agent.codex_runner.subprocess.run", fake_run)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    assert run_codex_prompt("plan", tmp_path, stream=False, backend="api") == "ok"

    env = captured["kwargs"]["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] == "codex-key"
    assert env["CODEX_API_KEY"] == "codex-key"


def test_streaming_prompt_reads_stdin_file_and_streams_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeStdout:
        def __iter__(self):
            return iter(['{"type":"message","message":"event"}\n'])

    class FakeStderr:
        def readlines(self) -> list[str]:
            return []

    class FakeProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            captured["command"] = command
            captured["kwargs"] = kwargs
            stdin = kwargs["stdin"]
            captured["input"] = stdin.read()
            captured["stdin_name"] = getattr(stdin, "name", "")
            self.command = command
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()

        def wait(self) -> int:
            output_path = Path(self.command[self.command.index("--output-last-message") + 1])
            output_path.write_text("stream final", encoding="utf-8")
            return 0

        def kill(self) -> None:
            captured["killed"] = True

    monkeypatch.setattr("agent.codex_runner.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    output = run_codex_prompt("plan", tmp_path, stream=True, timeout_seconds=17, phase="planner")

    assert output == "stream final"
    assert captured["input"].startswith("plan")
    assert Path(captured["stdin_name"]).name == "prompt.md"
    assert "--json" in captured["command"]


def test_streaming_timeout_kills_process_without_waiting_for_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    killed = threading.Event()

    class BlockingStdout:
        def __iter__(self):
            return self

        def __next__(self) -> str:
            killed.wait(timeout=1)
            raise StopIteration

    class FakeStderr:
        def readlines(self) -> list[str]:
            killed.wait(timeout=1)
            return []

    class FakeProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            self.stdout = BlockingStdout()
            self.stderr = FakeStderr()

        def wait(self) -> int:
            killed.wait(timeout=1)
            return 0

        def kill(self) -> None:
            killed.set()

    monkeypatch.setattr("agent.codex_runner.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    with pytest.raises(CodexRunnerError, match="timed out after"):
        run_codex_prompt(
            "plan",
            tmp_path,
            stream=True,
            timeout_seconds=0.01,
            phase="planner",
            transient_retries=0,
        )

    assert killed.is_set()


def test_captured_nonzero_result_raises_with_safe_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "agent.codex_runner.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 7, stdout="out", stderr="err"
        ),
    )

    with pytest.raises(CodexRunnerError) as error:
        run_codex_prompt("private prompt", tmp_path, stream=False, phase="fixer")

    message = str(error.value)
    assert "fixer" in message
    assert "return code 7" in message
    assert "private prompt" not in message


def test_looks_transient_matches_sandbox_spawn_failures() -> None:
    assert _looks_transient("error: failed to spawn sandbox helper")
    assert _looks_transient("The process cannot access the file")
    assert not _looks_transient("SyntaxError: invalid token at line 3")


def test_transient_failure_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="failed to spawn sandbox process"
            )
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("recovered answer", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("agent.codex_runner.subprocess.run", fake_run)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    output = run_codex_prompt("plan", tmp_path, stream=False, phase="planner")

    assert output == "recovered answer"
    assert len(calls) == 2


def test_transient_failure_propagates_after_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="failed to spawn sandbox process"
        )

    monkeypatch.setattr("agent.codex_runner.subprocess.run", fake_run)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    with pytest.raises(CodexTransientError):
        run_codex_prompt("plan", tmp_path, stream=False, phase="planner")

    assert len(calls) == 2


def test_non_transient_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(command, 7, stdout="out", stderr="syntax error")

    monkeypatch.setattr("agent.codex_runner.subprocess.run", fake_run)
    monkeypatch.setattr("agent.codex_runner._resolve_codex_command_prefix", lambda: ["codex.cmd"])

    with pytest.raises(CodexRunnerError) as error:
        run_codex_prompt("plan", tmp_path, stream=False, phase="fixer")

    assert not isinstance(error.value, CodexTransientError)
    assert len(calls) == 1


def test_max_budget_rejected_for_codex(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_budget_usd"):
        run_codex_prompt("plan", tmp_path, max_budget_usd=1.0)
