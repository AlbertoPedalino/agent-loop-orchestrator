"""Tests for backend-agnostic phase permission resolution."""

from agent.permissions import (
    blocked_commands_to_deny_rules,
    is_read_only_tool_set,
    resolve_phase_permissions,
)


def test_read_only_detection() -> None:
    assert is_read_only_tool_set(["Read", "Grep", "Glob"])
    assert not is_read_only_tool_set(["Read", "Edit"])
    assert not is_read_only_tool_set(["Bash"])


def test_deny_rules_skip_empty_and_dedupe() -> None:
    rules = blocked_commands_to_deny_rules(["git push", " ", "git push", "rm -rf"])

    assert rules == ["Bash(git push:*)", "Bash(rm -rf:*)"]


def test_resolve_phase_permissions_combines_inputs() -> None:
    permissions = resolve_phase_permissions(
        ["Read", "Edit", "Bash"], "acceptEdits", ["git push", "rm -rf"]
    )

    assert permissions.allowed_tools == ["Read", "Edit", "Bash"]
    assert permissions.disallowed_tools == ["Bash(git push:*)", "Bash(rm -rf:*)"]
    assert permissions.permission_mode == "acceptEdits"
