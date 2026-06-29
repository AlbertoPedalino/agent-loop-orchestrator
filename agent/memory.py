"""Accumulated project memory for the orchestration loop.

A target repository can keep a curated ``.agent-loop/memory.md`` holding what the
loop has learned about the project (architecture, file map, gotchas). The
orchestrator injects it into every phase prompt so a run verifies known structure
instead of re-exploring from scratch, and rewrites it from a fenced ``memory``
block the final read-only phase emits.

Design boundaries (kept deliberately narrow):

* Memory is *knowledge the loop discovers*; it is distinct from ``config.yaml``,
  which holds *human-authored policy* the loop enforces but never writes. The
  loop reads config and reads/writes memory — never the reverse.
* Memory lives in the main target repository (not a throwaway worktree) so it
  persists across runs and branches.
* The orchestrator performs the write deterministically from an extracted block;
  phases never edit the file directly. This keeps writes reviewable as a diff and
  keeps read-only phases read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

from agent.log import get_logger

DEFAULT_MEMORY_FILE = ".agent-loop/memory.md"

# Soft ceiling echoing the "keep it concise" guidance for agent context files;
# larger memory still works but is logged as a curation reminder.
_SOFT_MAX_LINES = 400

# A fenced ```memory ... ``` block. The last one in an output wins, so a phase can
# reason in prose and still finish with a single authoritative memory snapshot.
_MEMORY_BLOCK = re.compile(r"```memory[ \t]*\n(.*?)\n```", re.DOTALL)

MEMORY_UPDATE_INSTRUCTION = (
    "## Memory Update (required)\n\n"
    "End your reply with a single fenced block tagged `memory` containing the "
    "full, curated project memory in Markdown — not a diff. Merge what you just "
    "learned into the existing memory above:\n\n"
    "- Keep it concise (aim for under 300 lines) and deduplicated.\n"
    "- Record durable facts: architecture, entry points, key files, data flow, "
    "gotchas. Omit task-specific chatter.\n"
    "- Mark volatile details (exact line numbers, transient paths) as approximate.\n"
    "- Preserve still-correct existing memory; only revise what changed or is wrong.\n\n"
    "```memory\n# Project Memory\n...\n```\n"
)


@dataclass(frozen=True)
class MemoryConfig:
    """Resolved memory settings for one run."""

    enabled: bool
    path: Path


def resolve_memory_config(config: dict[str, Any], repo_path: Path) -> MemoryConfig:
    """Resolve the ``memory`` config section against the main repository path.

    Defaults to an enabled ``.agent-loop/memory.md`` so the feature works without
    configuration; set ``memory.enabled: false`` to opt out.
    """
    section = config.get("memory", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("Configuration 'memory' must be a YAML mapping")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("memory.enabled must be a boolean")
    file_value = section.get("file", DEFAULT_MEMORY_FILE)
    if not isinstance(file_value, str) or not file_value.strip():
        raise ValueError("memory.file must be a non-empty string")
    raw_path = Path(file_value).expanduser()
    resolved_repo = repo_path.expanduser().resolve()
    path = raw_path if raw_path.is_absolute() else resolved_repo / raw_path
    return MemoryConfig(enabled=enabled, path=path.resolve())


def load_memory(memory_config: MemoryConfig) -> str:
    """Return the current memory text, or ``""`` when disabled or absent."""
    if not memory_config.enabled or not memory_config.path.is_file():
        return ""
    return memory_config.path.read_text(encoding="utf-8").strip()


def format_memory_section(memory_text: str) -> str:
    """Format existing memory as a prompt section, or ``""`` when empty."""
    if not memory_text.strip():
        return ""
    return (
        "# Accumulated Project Memory\n\n"
        "Knowledge from previous agent-loop runs. Trust it as a starting point and "
        "verify only the areas relevant to this task instead of re-exploring the "
        "whole repository. Treat it as advisory, not authoritative over the code.\n\n"
        f"{memory_text.strip()}"
    )


def extract_memory_block(output: str) -> str | None:
    """Return the last fenced ``memory`` block's contents, or ``None`` if absent."""
    matches = _MEMORY_BLOCK.findall(output)
    if not matches:
        return None
    block = matches[-1].strip()
    return block or None


def write_memory(memory_config: MemoryConfig, content: str) -> None:
    """Write curated memory content to the configured path (creating parents)."""
    memory_config.path.parent.mkdir(parents=True, exist_ok=True)
    memory_config.path.write_text(content.strip() + "\n", encoding="utf-8")
    line_count = content.count("\n") + 1
    if line_count > _SOFT_MAX_LINES:
        get_logger().warning(
            "Project memory is %d lines (> %d); consider tightening it.",
            line_count,
            _SOFT_MAX_LINES,
        )


def update_memory_from_output(memory_config: MemoryConfig, output: str) -> bool:
    """Extract a memory block from *output* and persist it. Return whether written."""
    if not memory_config.enabled:
        return False
    block = extract_memory_block(output)
    if block is None:
        return False
    write_memory(memory_config, block)
    get_logger().info("Updated project memory: %s", memory_config.path)
    return True
