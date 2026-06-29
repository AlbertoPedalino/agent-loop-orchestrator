"""Provider-agnostic tool permissions for a single agent phase.

The orchestrator resolves one :class:`PhasePermissions` per phase and hands it to
whichever local CLI provider runs the phase. Claude receives allow/deny tool
policy, so read-only phases cannot edit and the configured blocked commands are
denied at the CLI tool boundary, not merely by prompt convention. Codex uses the
same phase mutability to select a read-only or workspace-write sandbox and
receives deny rules as prompt policy.

Enforcement strength differs by provider:

* The Claude CLI receives the same rules encoded as flags. CLI matching is prefix based,
  so denials are best-effort for commands that are chained or wrapped.
* Codex CLI does not expose equivalent allow/deny flags here; its deterministic
  boundary is the sandbox mode selected from the phase's write capability.

The deterministic verifier guard in :mod:`agent.policies` remains the source of
truth for the orchestrator's own verification commands regardless of backend.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tools capable of mutating the working tree or running arbitrary commands. A
# phase whose allowed tools include none of these is treated as read-only.
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})

VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions", "plan"})


@dataclass(frozen=True)
class PhasePermissions:
    """Resolved tool policy for one phase, shared across backends."""

    allowed_tools: list[str]
    disallowed_tools: list[str]
    permission_mode: str | None


def is_read_only_tool_set(allowed_tools: list[str]) -> bool:
    """Return whether an allowed-tool list grants no write-capable tools."""
    return not any(tool in WRITE_TOOLS for tool in allowed_tools)


def blocked_commands_to_deny_rules(blocked_commands: list[str]) -> list[str]:
    """Translate blocked-command substrings into backend tool deny rules.

    Each non-empty blocked command becomes a ``Bash(<command>:*)`` deny rule.
    Empty entries are ignored and order is preserved without duplicates.
    """
    rules: list[str] = []
    for blocked_command in blocked_commands:
        normalized = blocked_command.strip()
        rule = f"Bash({normalized}:*)"
        if normalized and rule not in rules:
            rules.append(rule)
    return rules


def resolve_phase_permissions(
    allowed_tools: list[str],
    permission_mode: str | None,
    blocked_commands: list[str],
) -> PhasePermissions:
    """Combine a phase's allowed tools, permission mode, and block list."""
    return PhasePermissions(
        allowed_tools=list(allowed_tools),
        disallowed_tools=blocked_commands_to_deny_rules(blocked_commands),
        permission_mode=permission_mode,
    )
