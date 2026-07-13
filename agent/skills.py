"""First-class skill policy for phase agents.

Skills are reusable instruction packages (``SKILL.md`` files) that the Claude
CLI discovers natively from the active repository (``.claude/skills/``), the
user scope, or installed plugins. Repository-local skills that are intentionally
untracked may be absent from an agent worktree; those are loaded from the source
checkout and inlined. Codex has no skill loader, so all skill bodies are inlined.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
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


def split_claude_worktree_skills(
    skills: list[SkillRef], active_repo: Path, source_repo: Path
) -> tuple[list[SkillRef], list[SkillRef]]:
    """Separate native Claude skills from source-only worktree skills.

    An ignored repository skill is not materialized by ``git worktree add``.
    When it exists in the source checkout but not the active worktree, return it
    for prompt inlining. Plugins, user-scoped skills, and skills already present
    in the worktree remain native Claude ``Skill`` invocations.
    """
    native: list[SkillRef] = []
    source_only: list[SkillRef] = []
    for ref in skills:
        active_file = active_repo / ".claude" / "skills" / ref.name / "SKILL.md"
        source_file = source_repo / ".claude" / "skills" / ref.name / "SKILL.md"
        if ":" not in ref.name and not active_file.is_file() and source_file.is_file():
            source_only.append(ref)
        else:
            native.append(ref)
    return native, source_only


def _default_claude_home() -> Path:
    return Path.home() / ".claude"


def _installed_plugin_skill_file(skill_name: str, claude_home: Path) -> Path:
    """Resolve ``plugin:skill`` to the installed plugin's SKILL.md.

    Uses the Claude CLI's own install manifest
    (``~/.claude/plugins/installed_plugins.json``, keys ``plugin@marketplace``)
    so codex phases read exactly the skill content a claude phase would load
    natively, without the user duplicating it into the repository.
    """
    plugin_name, _, bare_skill = skill_name.partition(":")
    manifest_path = claude_home / "plugins" / "installed_plugins.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Skill '{skill_name}' needs installed plugin '{plugin_name}', but no "
            f"plugin manifest was found at {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read plugin manifest {manifest_path}: {error}") from error
    entries = manifest.get("plugins", {}) if isinstance(manifest, dict) else {}
    for key, installs in entries.items():
        if not (isinstance(key, str) and key.split("@", 1)[0] == plugin_name):
            continue
        if not (isinstance(installs, list) and installs):
            continue
        install_path = installs[0].get("installPath") if isinstance(installs[0], dict) else None
        if not isinstance(install_path, str):
            continue
        candidate = Path(install_path) / "skills" / bare_skill / "SKILL.md"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(
            f"Plugin '{plugin_name}' is installed but has no skill '{bare_skill}': {candidate}"
        )
    raise FileNotFoundError(
        f"Skill '{skill_name}' needs plugin '{plugin_name}', which is not installed "
        f"(checked {manifest_path})"
    )


def _resolve_skill_file(skill_name: str, repo_path: Path, claude_home: Path) -> Path:
    """Find a skill's SKILL.md the way the Claude CLI would discover it.

    Plain names: target repository first, then user scope. Plugin-scoped names:
    the installed plugin's snapshot.
    """
    if ":" in skill_name:
        return _installed_plugin_skill_file(skill_name, claude_home)
    candidates = (
        repo_path / ".claude" / "skills" / skill_name / "SKILL.md",
        claude_home / "skills" / skill_name / "SKILL.md",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Skill '{skill_name}' not found; checked: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def inline_skills_for_codex(
    prompt: str,
    skills: list[SkillRef],
    repo_path: Path,
    claude_home: Path | None = None,
) -> str:
    """Prepend skill bodies to *prompt* for a backend without a skill loader.

    Skills are resolved from the same sources the Claude CLI uses (target
    repository, user scope, installed plugins), so the codex backend consumes
    the same content without the user duplicating existing skills.
    """
    resolved_home = claude_home if claude_home is not None else _default_claude_home()
    sections: list[str] = []
    reminders: list[str] = []
    for ref in skills:
        skill_path = _resolve_skill_file(ref.name, repo_path, resolved_home)
        body = skill_path.read_text(encoding="utf-8").strip()
        args_note = (
            f"\n\nMANDATORY for this phase: apply this skill in mode/level `{ref.args}`. "
            "Ignore any activation triggers or commands it mentions; it is already active."
            if ref.args
            else "\n\nMANDATORY for this phase: apply this skill. Ignore any activation "
            "triggers or commands it mentions; it is already active."
        )
        sections.append(f"## Skill: {ref.name}\n\n{body}{args_note}")
        reminders.append(f"`{ref.name}`" + (f" at `{ref.args}`" if ref.args else ""))
    header = (
        "# Required Skills\n\n"
        "The following skill instructions are mandatory for this phase, not optional "
        "context.\n\n"
    )
    # Restated after the task because instructions closest to the end of the
    # prompt are followed most reliably.
    reminder = (
        "\n\n---\n\nReminder: your final report MUST follow the required skill(s) "
        f"above: {', '.join(reminders)}."
    )
    return header + "\n\n".join(sections) + "\n\n---\n\n" + prompt + reminder
