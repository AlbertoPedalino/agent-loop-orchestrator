"""Tests for the per-phase skill policy helpers."""

from pathlib import Path

import pytest

from agent.skills import (
    SkillRef,
    allowed_tools_with_skill,
    format_skills_system_prompt,
    inline_skills_for_codex,
    validate_skills,
)


def test_validate_accepts_plain_and_plugin_scoped_names() -> None:
    assert validate_skills(None, "planner") == []
    assert validate_skills(["frontend-design", "caveman:caveman-review"], "planner") == [
        SkillRef("frontend-design"),
        SkillRef("caveman:caveman-review"),
    ]


def test_validate_accepts_mapping_entries_with_args() -> None:
    assert validate_skills(
        ["gmboard-ui", {"name": "caveman:caveman", "args": "wenyan-ultra"}], "planner"
    ) == [
        SkillRef("gmboard-ui"),
        SkillRef("caveman:caveman", args="wenyan-ultra"),
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
        [{"args": "wenyan-ultra"}],  # mapping without a name
        [{"name": "caveman:caveman", "args": ""}],  # empty args
        [{"name": "caveman:caveman", "level": "ultra"}],  # unknown key
    ],
)
def test_validate_rejects_malformed_entries(bad_skills: object) -> None:
    with pytest.raises(ValueError, match="planner"):
        validate_skills(bad_skills, "planner")


def test_validate_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="twice"):
        validate_skills(
            ["frontend-design", {"name": "frontend-design", "args": "x"}], "implementer"
        )


def test_allowed_tools_gain_skill_tool_once() -> None:
    assert allowed_tools_with_skill(["Read", "Grep"]) == ["Read", "Grep", "Skill"]
    assert allowed_tools_with_skill(["Read", "Skill"]) == ["Read", "Skill"]


def test_system_prompt_names_every_skill_and_its_args() -> None:
    instruction = format_skills_system_prompt(
        [SkillRef("frontend-design"), SkillRef("caveman:caveman", args="wenyan-ultra")]
    )

    assert "`frontend-design`" in instruction
    assert "`caveman:caveman` with args `wenyan-ultra`" in instruction
    assert "Skill tool" in instruction


def test_inline_for_codex_prepends_repo_local_skill_bodies(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "frontend-design"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Follow existing UI conventions.", encoding="utf-8")

    combined = inline_skills_for_codex(
        "Implement the change.", [SkillRef("frontend-design", args="dense")], tmp_path
    )

    assert combined.startswith("# Required Skills")
    assert "## Skill: frontend-design" in combined
    assert "Follow existing UI conventions." in combined
    assert "Apply with args: `dense`." in combined
    assert combined.rstrip().endswith("Implement the change.")


def test_inline_for_codex_rejects_missing_and_plugin_scoped_skills(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="frontend-design"):
        inline_skills_for_codex("task", [SkillRef("frontend-design")], tmp_path)

    with pytest.raises(ValueError, match="plugin-scoped"):
        inline_skills_for_codex("task", [SkillRef("caveman:caveman-review")], tmp_path)
