"""Configuration loader for the orchestrator's named phase agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent.agent_options import VALID_AGENT_PROVIDERS, VALID_BACKENDS
from agent.permissions import VALID_PERMISSION_MODES, is_read_only_tool_set
from agent.skills import SkillRef, validate_skills


# Optional per-target overlay, resolved inside the target repository. It merges
# field-by-field over the orchestrator defaults so a target can, for example,
# add skills to one phase without redefining prompts it does not own.
TARGET_OVERLAY_RELATIVE_PATH = Path(".agent-loop") / "subagents.yaml"

# Fields a target-local overlay may set. Permission-affecting fields
# (allowed_tools, permission_mode, agent, backend) are excluded so a target
# repository can customize instructions but never widen its own tool policy;
# full control requires an explicit --subagents-config chosen by the operator.
OVERLAY_ALLOWED_FIELDS = frozenset({"description", "prompt_template", "max_turns", "skills"})


@dataclass(frozen=True)
class SubagentConfig:
    name: str
    description: str
    allowed_tools: list[str]
    max_turns: int
    prompt_template: Path
    agent: str | None = None
    backend: str | None = None
    permission_mode: str | None = None
    skills: list[SkillRef] = field(default_factory=list)

    @property
    def is_read_only(self) -> bool:
        """Return whether this phase grants no write-capable tools."""
        return is_read_only_tool_set(self.allowed_tools)


def _required_string(entry: dict[str, Any], field: str, agent_name: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Subagent '{agent_name}' requires a non-empty '{field}' field")
    return value


def _load_raw_subagents(path: Path) -> dict[str, dict[str, Any]]:
    """Load a subagents YAML file and return its raw ``subagents`` mapping."""
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Subagents configuration file not found: {resolved_path}")
    with resolved_path.open(encoding="utf-8") as source:
        loaded = yaml.safe_load(source) or {}
    if not isinstance(loaded, dict) or not isinstance(loaded.get("subagents"), dict):
        raise ValueError(
            f"Subagents configuration requires a top-level 'subagents' mapping: {resolved_path}"
        )
    for name, entry in loaded["subagents"].items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError(f"Each subagent entry must be a named YAML mapping: {resolved_path}")
    return loaded["subagents"]


def _absolutize_prompt_template(entry: dict[str, Any], project_root: Path) -> dict[str, Any]:
    """Pin a relative ``prompt_template`` to the root of the file that declared it.

    Merged configurations mix entries from files with different roots (the
    orchestrator repository and the target repository), so relative prompt paths
    must be resolved before merging, not after.
    """
    value = entry.get("prompt_template")
    if isinstance(value, str) and value.strip() and not Path(value).is_absolute():
        return {**entry, "prompt_template": str((project_root / value).resolve())}
    return entry


def _parse_subagent_entry(name: str, entry: dict[str, Any]) -> SubagentConfig:
    """Validate one raw subagent mapping (with absolute prompt path) into a config."""
    description = _required_string(entry, "description", name)
    prompt_value = _required_string(entry, "prompt_template", name)
    tools = entry.get("allowed_tools")
    max_turns = entry.get("max_turns")
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        raise ValueError(f"Subagent '{name}' requires an 'allowed_tools' string list")
    if not isinstance(max_turns, int) or max_turns <= 0:
        raise ValueError(f"Subagent '{name}' requires a positive integer 'max_turns'")
    agent = entry.get("agent")
    if agent is not None and agent not in VALID_AGENT_PROVIDERS:
        raise ValueError(f"Subagent '{name}' agent must be 'claude' or 'codex'")
    backend = entry.get("backend")
    if backend is not None and backend not in VALID_BACKENDS:
        raise ValueError(f"Subagent '{name}' backend must be 'cli' or 'api'")
    permission_mode = entry.get("permission_mode")
    if permission_mode is not None and permission_mode not in VALID_PERMISSION_MODES:
        raise ValueError(
            f"Subagent '{name}' permission_mode must be one of: "
            f"{', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    skills = validate_skills(entry.get("skills"), name)
    prompt_path = Path(prompt_value).resolve()
    if not prompt_path.is_file():
        raise ValueError(f"Subagent '{name}' prompt template does not exist: {prompt_path}")
    return SubagentConfig(
        name=name,
        description=description,
        allowed_tools=tools,
        max_turns=max_turns,
        prompt_template=prompt_path,
        agent=agent,
        backend=backend,
        permission_mode=permission_mode,
        skills=skills,
    )


def load_subagents_config(path: Path) -> dict[str, SubagentConfig]:
    """Load subagent YAML, resolving prompt paths from the project root."""
    resolved_path = path.expanduser().resolve()
    project_root = resolved_path.parent.parent
    raw_subagents = _load_raw_subagents(resolved_path)
    return {
        name: _parse_subagent_entry(name, _absolutize_prompt_template(entry, project_root))
        for name, entry in raw_subagents.items()
    }


def load_subagents_with_target_overlay(
    default_path: Path, repo_path: Path
) -> tuple[dict[str, SubagentConfig], str]:
    """Load subagents, merging an optional target-local overlay over the defaults.

    The overlay lives at ``.agent-loop/subagents.yaml`` inside the target
    repository and overrides fields per subagent (an overlay entry may set only
    ``skills`` and inherit everything else). Overridden fields *replace* the
    default value — a ``skills`` list is not appended to the default one. Only
    :data:`OVERLAY_ALLOWED_FIELDS` may be set and only for subagents the
    defaults define, so an overlay can never widen tool permissions or add
    phases. The overlay is read from the source repository as launched, never
    from an agent worktree or branch, so agent-written branches cannot inject
    one. Relative prompt paths resolve against the file that declares them:
    defaults against the orchestrator repository, overlay entries against the
    target repository. The returned string describes the selection for run
    reporting.
    """
    overlay_path = repo_path.expanduser().resolve() / TARGET_OVERLAY_RELATIVE_PATH
    if not overlay_path.is_file():
        return load_subagents_config(default_path), f"orchestrator defaults ({default_path})"

    default_root = default_path.expanduser().resolve().parent.parent
    target_root = repo_path.expanduser().resolve()
    base_subagents = _load_raw_subagents(default_path)
    overlay_subagents = _load_raw_subagents(overlay_path)
    merged: dict[str, dict[str, Any]] = {
        name: _absolutize_prompt_template(entry, default_root)
        for name, entry in base_subagents.items()
    }
    for name, entry in overlay_subagents.items():
        if name not in merged:
            raise ValueError(
                f"Target overlay {overlay_path} defines unknown subagent '{name}'; "
                "an overlay may only customize subagents the defaults define"
            )
        denied_fields = sorted(set(entry) - OVERLAY_ALLOWED_FIELDS)
        if denied_fields:
            raise ValueError(
                f"Target overlay {overlay_path} sets restricted field(s) "
                f"{', '.join(denied_fields)} on subagent '{name}'; an overlay may set "
                f"only: {', '.join(sorted(OVERLAY_ALLOWED_FIELDS))}. "
                "Use an explicit --subagents-config for full control."
            )
        overlay_entry = _absolutize_prompt_template(entry, target_root)
        merged[name] = {**merged[name], **overlay_entry}
    configs = {name: _parse_subagent_entry(name, entry) for name, entry in merged.items()}
    return configs, f"defaults ({default_path}) with target overlay ({overlay_path})"
