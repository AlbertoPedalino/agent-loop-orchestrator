"""Tests for persistent per-project prompt context."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from agent import orchestrator
from agent.subagents import load_subagents_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "agent" / "resources" / "configs" / "default.yaml"
DEFAULT_SUBAGENTS = (
    PROJECT_ROOT / "agent" / "resources" / "configs" / "subagents.default.yaml"
)

PROJECT_CONTEXT = {
    "data_sources": {
        "data_repo": "https://example.test/data",
        "image_repo": "https://example.test/images",
    },
    "allowed_sources": ["XPHB", "XMM", "XDMG", "FRAIF", "FRHOF", "EFA", "RWH"],
    "rules": ["Do not vendor externally fetched assets."],
}


def test_target_project_context_loads_and_formats(tmp_path: Path) -> None:
    config_path = tmp_path / ".agent-loop" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        yaml.safe_dump({"project_context": PROJECT_CONTEXT}), encoding="utf-8"
    )
    config = orchestrator.load_config(config_path)
    project_context = orchestrator._resolve_project_context(config)
    formatted = orchestrator._format_project_context(project_context)

    assert project_context["allowed_sources"] == [
        "XPHB",
        "XMM",
        "XDMG",
        "FRAIF",
        "FRHOF",
        "EFA",
        "RWH",
    ]
    assert "# Persistent Project Context" in formatted
    assert "example.test/data" in formatted
    assert "Do not vendor externally fetched assets." in formatted


def test_prompt_omits_persistent_section_when_context_is_absent() -> None:
    subagent = load_subagents_config(DEFAULT_SUBAGENTS)["planner"]

    prompt = orchestrator._read_prompt(subagent, "specific task", PROJECT_ROOT)

    assert "# Persistent Project Context" not in prompt
    assert "specific task" in prompt


def test_all_phase_prompts_include_project_context() -> None:
    formatted = orchestrator._format_project_context(PROJECT_CONTEXT)
    subagents = load_subagents_config(DEFAULT_SUBAGENTS)

    for phase in ("planner", "implementer", "fixer", "reviewer"):
        prompt = orchestrator._read_prompt(
            subagents[phase],
            "specific task",
            PROJECT_ROOT,
            project_context=formatted,
        )
        assert "# Persistent Project Context" in prompt
        assert "XPHB" in prompt
        assert "example.test/images" in prompt


def test_dry_run_plan_only_records_project_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = deepcopy(orchestrator.load_config(DEFAULT_CONFIG))
    config["project_context"] = {
        "allowed_sources": ["XPHB"],
        "rules": ["Use the configured source whitelist."],
    }
    monkeypatch.setattr(orchestrator, "load_config", lambda path: config)
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "main")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="specific task",
        config_path=DEFAULT_CONFIG,
        config_source="target-local .agent-loop/config.yaml",
        dry_run=True,
        plan_only=True,
    )

    assert result.status == "dry-run-plan-only"
    assert "XPHB" in (run_dir / "project_context.md").read_text(encoding="utf-8")
    assert "# Persistent Project Context" in (
        run_dir / "planner_prompt.md"
    ).read_text(encoding="utf-8")
    assert "project_context:" in (run_dir / "config_snapshot.yaml").read_text(encoding="utf-8")
    assert "target-local .agent-loop/config.yaml" in (
        run_dir / "config_source.md"
    ).read_text(encoding="utf-8")
    assert "target-local .agent-loop/config.yaml" in (
        run_dir / "report.md"
    ).read_text(encoding="utf-8")


def test_full_pipeline_passes_context_to_every_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = deepcopy(orchestrator.load_config(DEFAULT_CONFIG))
    config["project_context"] = {"rules": ["Persistent test rule."]}
    prompts: dict[str, str] = {}
    verification_results = iter(
        [
            {"check": "exit code: 1\nstdout:\n\nstderr:\nfailure"},
            {"check": "exit code: 0\nstdout:\n\nstderr:\n"},
        ]
    )
    monkeypatch.setattr(orchestrator, "load_config", lambda path: config)
    monkeypatch.setattr(orchestrator, "ensure_git_repo", lambda path: True)
    monkeypatch.setattr(orchestrator, "get_current_branch", lambda path: "agent/test")
    monkeypatch.setattr(orchestrator, "get_git_status", lambda path: "")
    monkeypatch.setattr(orchestrator, "get_git_diff", lambda path: "")
    monkeypatch.setattr(orchestrator, "_create_run_dir", lambda path: run_dir)
    monkeypatch.setattr(
        orchestrator,
        "_run_phase",
        lambda *, phase, prompt, **kwargs: prompts.setdefault(phase, prompt) or f"{phase} output",
    )
    monkeypatch.setattr(
        orchestrator,
        "run_verification_commands",
        lambda *args, **kwargs: next(verification_results),
    )

    result = orchestrator.run_orchestrator(
        repo_path=tmp_path,
        task="specific task",
        config_path=DEFAULT_CONFIG,
    )

    assert result.status == "completed"
    assert set(prompts) == {"planner", "implementer", "fixer", "reviewer"}
    assert all("# Persistent Project Context" in prompt for prompt in prompts.values())
    assert all("Persistent test rule." in prompt for prompt in prompts.values())
