"""Shared agent provider and backend option names."""

from __future__ import annotations


VALID_AGENT_PROVIDERS = frozenset({"claude", "codex"})
VALID_BACKENDS = frozenset({"cli"})
