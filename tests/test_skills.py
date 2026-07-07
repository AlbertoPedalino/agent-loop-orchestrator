"""Tests for the per-phase skill policy helpers."""

from pathlib import Path
import json

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
    repo = tmp_path / "repo"
    skill_dir = repo / ".claude" / "skills" / "frontend-design"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Follow existing UI conventions.", encoding="utf-8")

    combined = inline_skills_for_codex(
        "Implement the change.",
        [SkillRef("frontend-design", args="dense")],
        repo,
        claude_home=tmp_path / "home" / ".claude",
    )

    assert combined.startswith("# Required Skills")
    assert "## Skill: frontend-design" in combined
    assert "Follow existing UI conventions." in combined
    assert "MANDATORY for this phase: apply this skill in mode/level `dense`." in combined
    assert "Implement the change." in combined
    # A compliance reminder is restated after the task for recency.
    assert combined.rstrip().endswith("`frontend-design` at `dense`.")


def test_inline_for_codex_falls_back_to_user_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude_home = tmp_path / ".claude"
    skill_dir = claude_home / "skills" / "my-style"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("User-scope style.", encoding="utf-8")

    combined = inline_skills_for_codex(
        "task", [SkillRef("my-style")], repo, claude_home=claude_home
    )

    assert "User-scope style." in combined


def _write_plugin_manifest(claude_home: Path, plugin: str, install_path: Path) -> None:
    manifest = {
        "version": 2,
        "plugins": {
            f"{plugin}@some-marketplace": [
                {"scope": "user", "installPath": str(install_path)}
            ]
        },
    }
    plugins_dir = claude_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_inline_for_codex_resolves_installed_plugin_skills(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude_home = tmp_path / ".claude"
    install_path = tmp_path / "plugins-cache" / "caveman" / "abc123"
    skill_dir = install_path / "skills" / "caveman"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Talk like caveman.", encoding="utf-8")
    _write_plugin_manifest(claude_home, "caveman", install_path)

    combined = inline_skills_for_codex(
        "task",
        [SkillRef("caveman:caveman", args="wenyan-ultra")],
        repo,
        claude_home=claude_home,
    )

    assert "## Skill: caveman:caveman" in combined
    assert "Talk like caveman." in combined
    assert "mode/level `wenyan-ultra`" in combined


def test_inline_for_codex_reports_missing_skills_clearly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude_home = tmp_path / ".claude"

    # Plain skill in neither the repo nor the user scope.
    with pytest.raises(FileNotFoundError, match="frontend-design"):
        inline_skills_for_codex(
            "task", [SkillRef("frontend-design")], repo, claude_home=claude_home
        )

    # Plugin-scoped skill without any plugin manifest.
    with pytest.raises(FileNotFoundError, match="manifest"):
        inline_skills_for_codex(
            "task", [SkillRef("caveman:caveman")], repo, claude_home=claude_home
        )

    # Manifest exists but the plugin is not installed.
    _write_plugin_manifest(claude_home, "other-plugin", tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError, match="not installed"):
        inline_skills_for_codex(
            "task", [SkillRef("caveman:caveman")], repo, claude_home=claude_home
        )

    # Plugin installed but the named skill is missing from it.
    install_path = tmp_path / "plugins-cache" / "caveman" / "abc123"
    (install_path / "skills").mkdir(parents=True)
    _write_plugin_manifest(claude_home, "caveman", install_path)
    with pytest.raises(FileNotFoundError, match="no skill 'caveman'"):
        inline_skills_for_codex(
            "task", [SkillRef("caveman:caveman")], repo, claude_home=claude_home
        )
