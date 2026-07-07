"""Tests for the per-phase skill policy helpers."""

from pathlib import Path

import pytest

from agent.skills import (
    allowed_tools_with_skill,
    format_skills_system_prompt,
    inline_skills_for_codex,
    validate_skill_names,
)


def test_validate_accepts_plain_and_plugin_scoped_names() -> None:
    assert validate_skill_names(None, "planner") == []
    assert validate_skill_names(["frontend-design", "caveman:caveman-review"], "planner") == [
        "frontend-design",
        "caveman:caveman-review",
    ]


@pytest.mark.parametrize(
    "bad_skills",
    [
        "frontend-design",  # not a list
        [42],
        ["Frontend"],  # uppercase
        ["-frontend"],  # bad leading character
        ["a:b:c"],  # too many segments
        [""],
    ],
)
def test_validate_rejects_malformed_entries(bad_skills: object) -> None:
    with pytest.raises(ValueError, match="planner"):
        validate_skill_names(bad_skills, "planner")


def test_validate_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="twice"):
        validate_skill_names(["frontend-design", "frontend-design"], "implementer")


def test_allowed_tools_gain_skill_tool_once() -> None:
    assert allowed_tools_with_skill(["Read", "Grep"]) == ["Read", "Grep", "Skill"]
    assert allowed_tools_with_skill(["Read", "Skill"]) == ["Read", "Skill"]


def test_system_prompt_names_every_skill() -> None:
    instruction = format_skills_system_prompt(["frontend-design", "caveman:caveman-review"])

    assert "`frontend-design`" in instruction
    assert "`caveman:caveman-review`" in instruction
    assert "Skill tool" in instruction


def test_inline_for_codex_prepends_repo_local_skill_bodies(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "frontend-design"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Follow existing UI conventions.", encoding="utf-8")

    combined = inline_skills_for_codex("Implement the change.", ["frontend-design"], tmp_path)

    assert combined.startswith("# Required Skills")
    assert "## Skill: frontend-design" in combined
    assert "Follow existing UI conventions." in combined
    assert combined.rstrip().endswith("Implement the change.")


def test_inline_for_codex_rejects_missing_and_plugin_scoped_skills(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="frontend-design"):
        inline_skills_for_codex("task", ["frontend-design"], tmp_path)

    with pytest.raises(ValueError, match="plugin-scoped"):
        inline_skills_for_codex("task", ["caveman:caveman-review"], tmp_path)
