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

from dataclasses import dataclass
from pathlib import Path
import re

# Tool the Claude CLI uses to invoke a discovered skill. It loads instructions
# only, so granting it does not make a read-only phase write-capable.
SKILL_TOOL = "Skill"

# `name` or `plugin:name`, lowercase kebab-case segments.
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*(:[a-z0-9][a-z0-9-]*)?$")


@dataclass(frozen=True)
class SkillRef:
    """One declared skill: its name plus optional invocation arguments.

    Arguments let a phase request a skill mode (e.g. an intensity level) that
    the skill itself defines; the orchestrator passes them through verbatim.
    """

    name: str
    args: str | None = None

    def __str__(self) -> str:
        return self.name if self.args is None else f"{self.name} (args: {self.args})"


def validate_skills(skills: object, agent_name: str) -> list[SkillRef]:
    """Validate a subagent's ``skills`` entry and return it as skill references.

    Each entry is either a plain name string or a ``{name, args}`` mapping. The
    orchestrator cannot know every skill available to the CLI (user scope and
    plugins are invisible to it), so validation is shape-only: well-formed
    ``name`` or ``plugin:name`` identifiers without duplicates.
    """
    if skills is None:
        return []
    if not isinstance(skills, list):
        raise ValueError(f"Subagent '{agent_name}' 'skills' must be a list")
    validated: list[SkillRef] = []
    seen_names: set[str] = set()
    for entry in skills:
        if isinstance(entry, str):
            name, args = entry, None
        elif isinstance(entry, dict):
            name = entry.get("name")
            args = entry.get("args")
            unknown_keys = sorted(set(entry) - {"name", "args"})
            if unknown_keys:
                raise ValueError(
                    f"Subagent '{agent_name}' skill entry has unknown key(s): "
                    f"{', '.join(unknown_keys)}"
                )
            if args is not None and (not isinstance(args, str) or not args.strip()):
                raise ValueError(
                    f"Subagent '{agent_name}' skill 'args' must be a non-empty string"
                )
        else:
            raise ValueError(
                f"Subagent '{agent_name}' skill entries must be strings or "
                "{name, args} mappings"
            )
        if not isinstance(name, str) or not _SKILL_NAME_PATTERN.match(name.strip()):
            raise ValueError(
                f"Subagent '{agent_name}' skill '{name}' must match "
                "'name' or 'plugin:name' in lowercase kebab-case"
            )
        normalized = name.strip()
        if normalized in seen_names:
            raise ValueError(f"Subagent '{agent_name}' lists skill '{normalized}' twice")
        seen_names.add(normalized)
        validated.append(SkillRef(name=normalized, args=args.strip() if args else None))
    return validated


def allowed_tools_with_skill(allowed_tools: list[str]) -> list[str]:
    """Return the tool list with the ``Skill`` tool granted.

    Print mode denies tools outside the allowlist, so without this grant a
    declared skill would be discovered by the CLI but never invocable.
    """
    if SKILL_TOOL in allowed_tools:
        return list(allowed_tools)
    return [*allowed_tools, SKILL_TOOL]


def format_skills_system_prompt(skills: list[SkillRef]) -> str:
    """Build the system-prompt instruction that makes a phase use its skills."""
    parts = []
    for ref in skills:
        if ref.args is None:
            parts.append(f"`{ref.name}`")
        else:
            parts.append(f"`{ref.name}` with args `{ref.args}`")
    names = ", ".join(parts)
    return (
        f"Required skills for this phase: {names}. "
        "Invoke each of them via the Skill tool (passing the indicated args) "
        "before doing the related work. "
        "If a skill is unavailable in this environment, say so in your report "
        "and continue without it."
    )


def _skill_file(repo_path: Path, skill_name: str) -> Path:
    return repo_path / ".claude" / "skills" / skill_name / "SKILL.md"


def inline_skills_for_codex(prompt: str, skills: list[SkillRef], repo_path: Path) -> str:
    """Prepend skill bodies to *prompt* for a backend without a skill loader.

    Only repository-local skills can be resolved; a ``plugin:name`` skill has no
    path the orchestrator can read, so it is rejected rather than silently
    dropped.
    """
    sections: list[str] = []
    for ref in skills:
        if ":" in ref.name:
            raise ValueError(
                f"Skill '{ref.name}' is plugin-scoped and cannot be inlined for the "
                "codex backend; use a repository-local skill or the claude backend."
            )
        skill_path = _skill_file(repo_path, ref.name)
        if not skill_path.is_file():
            raise FileNotFoundError(
                f"Skill '{ref.name}' not found for the codex backend: {skill_path}"
            )
        body = skill_path.read_text(encoding="utf-8").strip()
        args_note = f"\n\nApply with args: `{ref.args}`." if ref.args else ""
        sections.append(f"## Skill: {ref.name}\n\n{body}{args_note}")
    header = (
        "# Required Skills\n\n"
        "Apply the following skill instructions throughout this phase.\n\n"
    )
    return header + "\n\n".join(sections) + "\n\n---\n\n" + prompt
