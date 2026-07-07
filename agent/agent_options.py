"""Shared agent provider and backend option names."""

from __future__ import annotations


VALID_AGENT_PROVIDERS = frozenset({"claude", "codex"})
# Both backends execute the provider's local CLI; they differ only in auth:
# "cli" uses the CLI's subscription login, "api" injects an API key so the run
# bills the API account (see agent/api_keys.py).
VALID_BACKENDS = frozenset({"cli", "api"})
