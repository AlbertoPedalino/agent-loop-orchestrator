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


def test_failed_phase_is_recorded_in_phase_events(
    monkeypatch: "pytest.MonkeyPatch", tmp_path: "Path"
) -> None:
    """A phase that raises must still leave a status=failed event for auditing."""
    import json

    from agent import orchestrator
    from agent.subagents import SubagentConfig

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prompt_path = tmp_path / "planner.md"
    prompt_path.write_text("# Planner", encoding="utf-8")
    subagent = SubagentConfig(
        name="planner",
        description="plan",
        allowed_tools=["Read"],
        prompt_template=prompt_path,
    )
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "feat/x")
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("agent exploded")),
    )

    with pytest.raises(RuntimeError, match="agent exploded"):
        orchestrator._run_phase(
            phase="planner",
            prompt="p",
            task="t",
            repo_path=tmp_path,
            agent="claude",
            backend="cli",
            subagent=subagent,
            max_budget_usd=None,
            run_dir=run_dir,
        )

    events = [
        json.loads(line)
        for line in (run_dir / "phase_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["error"] == "agent exploded"
    assert events[0]["phase"] == "planner"
    assert "duration_seconds" in events[0]


# --- custom hook registry ---

from agent.hooks import (  # noqa: E402
    HookCommand,
    HooksConfig,
    load_hooks_config,
    run_custom_hooks,
)


def test_load_hooks_defaults_to_empty() -> None:
    config = load_hooks_config({}, [])

    assert config.is_empty
    assert config.for_event("pre_phase") == []


def test_load_hooks_parses_string_and_mapping_entries() -> None:
    config = load_hooks_config(
        {
            "hooks": {
                "pre_phase": ["python guard.py"],
                "post_phase": [{"command": "python notify.py", "timeout_seconds": 5}],
            }
        },
        [],
    )

    assert config.for_event("pre_phase") == [HookCommand("python guard.py")]
    assert config.for_event("post_phase") == [HookCommand("python notify.py", timeout_seconds=5)]


@pytest.mark.parametrize(
    "raw",
    [
        {"hooks": {"on_boot": ["x"]}},  # unknown event
        {"hooks": {"pre_phase": "not-a-list"}},
        {"hooks": {"pre_phase": [{"command": ""}]}},
        {"hooks": {"pre_phase": [{"command": "x", "timeout_seconds": 0}]}},
        {"hooks": {"pre_phase": [{"command": "x", "when": "always"}]}},  # unknown key
    ],
)
def test_load_hooks_rejects_malformed_config(raw: dict) -> None:
    with pytest.raises(ValueError):
        load_hooks_config(raw, [])


def test_load_hooks_enforces_blocked_commands() -> None:
    with pytest.raises(ValueError, match="blocked"):
        load_hooks_config({"hooks": {"post_phase": ["git push origin main"]}}, ["git push"])


def test_gating_hook_failure_raises_and_post_hook_failure_does_not(tmp_path: Path) -> None:
    failing = HooksConfig(hooks={
        "pre_phase": [HookCommand("python -c exit(3)")],
        "post_phase": [HookCommand("python -c exit(3)")],
    })

    with pytest.raises(HookViolationError, match="pre_phase"):
        run_custom_hooks("pre_phase", failing, tmp_path)

    # Informational event: same failing command must not raise.
    run_custom_hooks("post_phase", failing, tmp_path)


def test_hooks_receive_agent_loop_context_env(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        "ok = os.environ.get('AGENT_LOOP_PHASE') == 'planner'\n"
        "ok = ok and os.environ.get('AGENT_LOOP_EVENT') == 'pre_phase'\n"
        "sys.exit(0 if ok else 4)\n",
        encoding="utf-8",
    )
    config = HooksConfig(hooks={"pre_phase": [HookCommand(f"python {probe}")]})

    # Passes only when the context env vars are present with the right values.
    run_custom_hooks("pre_phase", config, tmp_path, {"phase": "planner"})


def test_missing_hook_executable_gates_closed(tmp_path: Path) -> None:
    config = HooksConfig(hooks={"pre_command": [HookCommand("definitely-not-a-command-xyz")]})

    with pytest.raises(HookViolationError, match="could not run"):
        run_custom_hooks("pre_command", config, tmp_path)


def test_pre_phase_hook_veto_is_recorded_as_failed_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    from agent import orchestrator
    from agent.subagents import SubagentConfig

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prompt_path = tmp_path / "planner.md"
    prompt_path.write_text("# Planner", encoding="utf-8")
    subagent = SubagentConfig(
        name="planner",
        description="plan",
        allowed_tools=["Read"],
        prompt_template=prompt_path,
    )
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "feat/x")
    monkeypatch.setattr(
        orchestrator,
        "run_claude_prompt",
        lambda *args, **kwargs: pytest.fail("a vetoed phase must not reach the agent"),
    )
    veto = HooksConfig(hooks={"pre_phase": [HookCommand("python -c exit(1)")]})

    with pytest.raises(HookViolationError):
        orchestrator._run_phase(
            phase="planner",
            prompt="p",
            task="t",
            repo_path=tmp_path,
            agent="claude",
            backend="cli",
            subagent=subagent,
            max_budget_usd=None,
            run_dir=run_dir,
            hooks_config=veto,
        )

    events = [
        json.loads(line)
        for line in (run_dir / "phase_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "pre_phase" in events[0]["error"]
