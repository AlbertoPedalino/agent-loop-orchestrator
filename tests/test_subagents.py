"""Tests for subagent configuration loading."""

from pathlib import Path

import pytest

from agent.subagents import load_subagents_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_default_subagents_load_with_read_only_roles() -> None:
    configs = load_subagents_config(PROJECT_ROOT / "configs" / "subagents.default.yaml")

    assert set(configs) == {"planner", "implementer", "fixer", "reviewer"}
    assert configs["planner"].prompt_template == PROJECT_ROOT / "prompts" / "planner.md"
    assert "Edit" not in configs["planner"].allowed_tools
    assert "Edit" not in configs["reviewer"].allowed_tools


def test_missing_subagent_field_fails_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "invalid.yaml"
    config_path.parent.mkdir()
    config_path.write_text("subagents:\n  planner:\n    description: plan\n", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt_template"):
        load_subagents_config(config_path)
