"""First-class skill policy for phase agents.

Skills are reusable instruction packages (``SKILL.md`` files) that the Claude
CLI discovers natively from the target repository (``.claude/skills/``), the
user scope, or installed plugins. The orchestrator therefore never loads skill
content for the Claude backend: declaring ``skills:`` on a phase grants the
``Skill`` tool and appends a system-prompt instruction to invoke the named
skills. Codex has no skill loader, so for that backend the skill body is read
from the target repository and inlined into the phase prompt instead.
"""

from __future__ import annotations

from pathlib import Path
import re

# Tool the Claude CLI uses to invoke a discovered skill. It loads instructions
# only, so granting it does not make a read-only phase write-capable.
SKILL_TOOL = "Skill"

# `name` or `plugin:name`, lowercase kebab-case segments.
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*(:[a-z0-9][a-z0-9-]*)?$")


def validate_skill_names(skills: object, agent_name: str) -> list[str]:
    """Validate a subagent's ``skills`` entry and return it as a list.

    The orchestrator cannot know every skill available to the CLI (user scope
    and plugins are invisible to it), so validation is shape-only: a list of
    well-formed ``name`` or ``plugin:name`` identifiers without duplicates.
    """
    if skills is None:
        return []
    if not isinstance(skills, list) or not all(isinstance(skill, str) for skill in skills):
        raise ValueError(f"Subagent '{agent_name}' 'skills' must be a list of strings")
    validated: list[str] = []
    for skill in skills:
        normalized = skill.strip()
        if not _SKILL_NAME_PATTERN.match(normalized):
            raise ValueError(
                f"Subagent '{agent_name}' skill '{skill}' must match "
                "'name' or 'plugin:name' in lowercase kebab-case"
            )
        if normalized in validated:
            raise ValueError(f"Subagent '{agent_name}' lists skill '{normalized}' twice")
        validated.append(normalized)
    return validated


def allowed_tools_with_skill(allowed_tools: list[str]) -> list[str]:
    """Return the tool list with the ``Skill`` tool granted.

    Print mode denies tools outside the allowlist, so without this grant a
    declared skill would be discovered by the CLI but never invocable.
    """
    if SKILL_TOOL in allowed_tools:
        return list(allowed_tools)
    return [*allowed_tools, SKILL_TOOL]


def format_skills_system_prompt(skills: list[str]) -> str:
    """Build the system-prompt instruction that makes a phase use its skills."""
    names = ", ".join(f"`{skill}`" for skill in skills)
    return (
        f"Required skills for this phase: {names}. "
        "Invoke each of them via the Skill tool before doing the related work. "
        "If a skill is unavailable in this environment, say so in your report "
        "and continue without it."
    )


def _skill_file(repo_path: Path, skill: str) -> Path:
    return repo_path / ".claude" / "skills" / skill / "SKILL.md"


def inline_skills_for_codex(prompt: str, skills: list[str], repo_path: Path) -> str:
    """Prepend skill bodies to *prompt* for a backend without a skill loader.

    Only repository-local skills can be resolved; a ``plugin:name`` skill has no
    path the orchestrator can read, so it is rejected rather than silently
    dropped.
    """
    sections: list[str] = []
    for skill in skills:
        if ":" in skill:
            raise ValueError(
                f"Skill '{skill}' is plugin-scoped and cannot be inlined for the "
                "codex backend; use a repository-local skill or the claude backend."
            )
        skill_path = _skill_file(repo_path, skill)
        if not skill_path.is_file():
            raise FileNotFoundError(
                f"Skill '{skill}' not found for the codex backend: {skill_path}"
            )
        sections.append(f"## Skill: {skill}\n\n{skill_path.read_text(encoding='utf-8').strip()}")
    header = (
        "# Required Skills\n\n"
        "Apply the following skill instructions throughout this phase.\n\n"
    )
    return header + "\n\n".join(sections) + "\n\n---\n\n" + prompt
