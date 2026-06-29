"""Configuration loader for the orchestrator's named phase agents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent.permissions import VALID_PERMISSION_MODES, is_read_only_tool_set


@dataclass(frozen=True)
class SubagentConfig:
    name: str
    description: str
    allowed_tools: list[str]
    max_turns: int
    prompt_template: Path
    backend: str | None = None
    permission_mode: str | None = None

    @property
    def is_read_only(self) -> bool:
        """Return whether this phase grants no write-capable tools."""
        return is_read_only_tool_set(self.allowed_tools)


def _required_string(entry: dict[str, Any], field: str, agent_name: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Subagent '{agent_name}' requires a non-empty '{field}' field")
    return value


def load_subagents_config(path: Path) -> dict[str, SubagentConfig]:
    """Load subagent YAML, resolving prompt paths from the project root."""
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Subagents configuration file not found: {resolved_path}")
    with resolved_path.open(encoding="utf-8") as source:
        loaded = yaml.safe_load(source) or {}
    if not isinstance(loaded, dict) or not isinstance(loaded.get("subagents"), dict):
        raise ValueError("Subagents configuration requires a top-level 'subagents' mapping")

    project_root = resolved_path.parent.parent
    configs: dict[str, SubagentConfig] = {}
    for name, entry in loaded["subagents"].items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("Each subagent entry must be a named YAML mapping")
        description = _required_string(entry, "description", name)
        prompt_value = _required_string(entry, "prompt_template", name)
        tools = entry.get("allowed_tools")
        max_turns = entry.get("max_turns")
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError(f"Subagent '{name}' requires an 'allowed_tools' string list")
        if not isinstance(max_turns, int) or max_turns <= 0:
            raise ValueError(f"Subagent '{name}' requires a positive integer 'max_turns'")
        raw_prompt_path = Path(prompt_value)
        prompt_path = raw_prompt_path if raw_prompt_path.is_absolute() else project_root / raw_prompt_path
        prompt_path = prompt_path.resolve()
        if not prompt_path.is_file():
            raise ValueError(f"Subagent '{name}' prompt template does not exist: {prompt_path}")
        backend = entry.get("backend")
        if backend is not None and backend not in {"cli", "sdk"}:
            raise ValueError(f"Subagent '{name}' backend must be 'cli' or 'sdk'")
        permission_mode = entry.get("permission_mode")
        if permission_mode is not None and permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"Subagent '{name}' permission_mode must be one of: "
                f"{', '.join(sorted(VALID_PERMISSION_MODES))}"
            )
        configs[name] = SubagentConfig(
            name=name,
            description=description,
            allowed_tools=tools,
            max_turns=max_turns,
            prompt_template=prompt_path,
            backend=backend,
            permission_mode=permission_mode,
        )
    return configs
