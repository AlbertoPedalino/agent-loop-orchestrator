"""Tests for subagent configuration loading."""

from pathlib import Path

import pytest

from agent.skills import SkillRef
from agent.subagents import load_subagents_config, load_subagents_with_target_overlay


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_default_subagents_load_with_read_only_roles() -> None:
    configs = load_subagents_config(PROJECT_ROOT / "configs" / "subagents.default.yaml")

    assert set(configs) == {"planner", "implementer", "fixer", "reviewer"}
    assert configs["planner"].prompt_template == PROJECT_ROOT / "prompts" / "planner.md"
    assert "Edit" not in configs["planner"].allowed_tools
    assert "Edit" not in configs["reviewer"].allowed_tools
    assert configs["planner"].is_read_only
    assert configs["reviewer"].is_read_only
    assert not configs["implementer"].is_read_only
    assert not configs["fixer"].is_read_only
    assert configs["planner"].agent is None
    assert configs["planner"].permission_mode is None
    assert configs["implementer"].permission_mode == "acceptEdits"
    assert configs["fixer"].permission_mode == "acceptEdits"
    assert all(config.skills == [] for config in configs.values())


def test_skills_are_parsed_and_validated(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "skills.yaml"
    config_path.parent.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "implementer.md").write_text("# Implementer", encoding="utf-8")
    config_path.write_text(
        "subagents:\n"
        "  implementer:\n"
        "    description: edit\n"
        "    allowed_tools: [Read, Edit]\n"
        "    max_turns: 4\n"
        "    prompt_template: prompts/implementer.md\n"
        "    skills:\n"
        "      - frontend-design\n"
        "      - caveman:caveman-review\n",
        encoding="utf-8",
    )

    configs = load_subagents_config(config_path)

    assert configs["implementer"].skills == [
        SkillRef("frontend-design"),
        SkillRef("caveman:caveman-review"),
    ]


def test_invalid_skill_name_fails_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "bad-skill.yaml"
    config_path.parent.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "planner.md").write_text("# Planner", encoding="utf-8")
    config_path.write_text(
        "subagents:\n"
        "  planner:\n"
        "    description: plan\n"
        "    allowed_tools: [Read]\n"
        "    max_turns: 2\n"
        "    prompt_template: prompts/planner.md\n"
        "    skills: [Not A Skill]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="skill"):
        load_subagents_config(config_path)


def test_invalid_permission_mode_fails_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "bad-mode.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "subagents:\n"
        "  planner:\n"
        "    description: plan\n"
        "    allowed_tools: [Read]\n"
        "    max_turns: 2\n"
        "    prompt_template: prompts/planner.md\n"
        "    permission_mode: yolo\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="permission_mode"):
        load_subagents_config(config_path)


def test_invalid_agent_fails_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "bad-agent.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "subagents:\n"
        "  planner:\n"
        "    description: plan\n"
        "    allowed_tools: [Read]\n"
        "    max_turns: 2\n"
        "    prompt_template: prompts/planner.md\n"
        "    agent: bogus\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="agent"):
        load_subagents_config(config_path)


DEFAULTS_PATH = PROJECT_ROOT / "configs" / "subagents.default.yaml"


def test_no_overlay_falls_back_to_defaults(tmp_path: Path) -> None:
    configs, source = load_subagents_with_target_overlay(DEFAULTS_PATH, tmp_path)

    assert set(configs) == {"planner", "implementer", "fixer", "reviewer"}
    assert "orchestrator defaults" in source


def test_target_overlay_adds_skills_and_inherits_the_rest(tmp_path: Path) -> None:
    overlay_dir = tmp_path / ".agent-loop"
    overlay_dir.mkdir()
    (overlay_dir / "subagents.yaml").write_text(
        "subagents:\n"
        "  implementer:\n"
        "    skills:\n"
        "      - frontend-design\n",
        encoding="utf-8",
    )

    configs, source = load_subagents_with_target_overlay(DEFAULTS_PATH, tmp_path)

    assert configs["implementer"].skills == [SkillRef("frontend-design")]
    # Everything not overridden is inherited from the orchestrator defaults.
    assert configs["implementer"].permission_mode == "acceptEdits"
    assert configs["implementer"].prompt_template == PROJECT_ROOT / "prompts" / "implementer.md"
    assert configs["planner"].skills == []
    assert "target overlay" in source


def test_target_overlay_prompt_paths_resolve_against_the_target(tmp_path: Path) -> None:
    overlay_dir = tmp_path / ".agent-loop"
    prompts_dir = overlay_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "implementer.md").write_text("# Target Implementer", encoding="utf-8")
    (overlay_dir / "subagents.yaml").write_text(
        "subagents:\n"
        "  implementer:\n"
        "    prompt_template: .agent-loop/prompts/implementer.md\n",
        encoding="utf-8",
    )

    configs, _ = load_subagents_with_target_overlay(DEFAULTS_PATH, tmp_path)

    assert configs["implementer"].prompt_template == (prompts_dir / "implementer.md").resolve()


@pytest.mark.parametrize(
    "field_line",
    [
        "    permission_mode: bypassPermissions\n",
        "    allowed_tools: [Bash]\n",
        "    agent: codex\n",
        "    backend: cli\n",
    ],
)
def test_target_overlay_cannot_touch_permission_fields(tmp_path: Path, field_line: str) -> None:
    overlay_dir = tmp_path / ".agent-loop"
    overlay_dir.mkdir()
    (overlay_dir / "subagents.yaml").write_text(
        "subagents:\n  implementer:\n" + field_line,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="restricted field"):
        load_subagents_with_target_overlay(DEFAULTS_PATH, tmp_path)


def test_target_overlay_cannot_add_new_subagents(tmp_path: Path) -> None:
    overlay_dir = tmp_path / ".agent-loop"
    overlay_dir.mkdir()
    (overlay_dir / "subagents.yaml").write_text(
        "subagents:\n  deployer:\n    skills: [frontend-design]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown subagent 'deployer'"):
        load_subagents_with_target_overlay(DEFAULTS_PATH, tmp_path)


def test_target_overlay_rejects_invalid_fields(tmp_path: Path) -> None:
    overlay_dir = tmp_path / ".agent-loop"
    overlay_dir.mkdir()
    (overlay_dir / "subagents.yaml").write_text(
        "subagents:\n"
        "  implementer:\n"
        "    skills: [Not Valid]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="skill"):
        load_subagents_with_target_overlay(DEFAULTS_PATH, tmp_path)


def test_missing_subagent_field_fails_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "invalid.yaml"
    config_path.parent.mkdir()
    config_path.write_text("subagents:\n  planner:\n    description: plan\n", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt_template"):
        load_subagents_config(config_path)
