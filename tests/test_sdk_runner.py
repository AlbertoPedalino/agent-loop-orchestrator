"""Tests for the lazy SDK adapter without an installed SDK."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.sdk_runner import AgentSdkUnavailableError, run_agent_sdk_prompt_sync


def test_sdk_unavailable_is_clear(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing_sdk(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("agent.sdk_runner.importlib.import_module", missing_sdk)

    with pytest.raises(AgentSdkUnavailableError, match="Install with"):
        run_agent_sdk_prompt_sync("plan", tmp_path)


def test_sdk_adapter_collects_mocked_async_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Options:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    async def stream() -> object:
        yield SimpleNamespace(text="first")
        yield SimpleNamespace(result="second")

    def query(**kwargs: object) -> object:
        captured["query"] = kwargs
        return stream()

    fake_sdk = SimpleNamespace(query=query, ClaudeAgentOptions=Options)
    monkeypatch.setattr("agent.sdk_runner.importlib.import_module", lambda name: fake_sdk)

    output = run_agent_sdk_prompt_sync(
        "plan",
        tmp_path,
        allowed_tools=["Read"],
        disallowed_tools=["Bash(git push:*)"],
        max_turns=2,
        permission_mode="acceptEdits",
        phase="planner",
    )

    assert output == "first\nsecond"
    assert captured["cwd"] == str(tmp_path.resolve())
    assert captured["allowed_tools"] == ["Read"]
    assert captured["disallowed_tools"] == ["Bash(git push:*)"]
    assert captured["max_turns"] == 2
    assert captured["permission_mode"] == "acceptEdits"
