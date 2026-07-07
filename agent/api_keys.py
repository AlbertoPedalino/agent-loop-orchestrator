"""API-key billing support for the local CLI backends.

Both providers keep executing through their local CLI (``claude -p`` /
``codex exec``); the backend only selects how that CLI authenticates:

* ``backend: cli`` — subscription auth. The CLI uses its own login state, and
  the provider's API-key environment variables are stripped from the
  subprocess environment so a stray exported key cannot silently switch a run
  to API billing.
* ``backend: api`` — API-key auth. The resolved key is injected into the CLI
  subprocess environment (``ANTHROPIC_API_KEY`` for Claude Code,
  ``OPENAI_API_KEY``/``CODEX_API_KEY`` for Codex), so the run bills the API
  account instead of a subscription plan.

Key resolution order: the orchestrator process environment first, then the
orchestrator-root ``.env`` file (gitignored; see ``.env.example``). The target
repository is never consulted, so a checked-out branch cannot inject
credentials.
"""

from __future__ import annotations

from pathlib import Path
import os


class ApiKeyError(RuntimeError):
    """Raised when backend 'api' is selected but no API key can be resolved."""


# The variable(s) the provider CLI reads for API-key auth. The first entry is
# the canonical lookup name; every entry is set on injection and stripped in
# subscription mode.
_PROVIDER_KEY_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
}

# Additional auth variables stripped in both modes' non-selected direction:
# ANTHROPIC_AUTH_TOKEN conflicts with ANTHROPIC_API_KEY when both are set.
_PROVIDER_STRIP_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
}


def _orchestrator_env_file() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` .env file (no dependency on python-dotenv).

    Blank lines and ``#`` comments are ignored; an optional ``export `` prefix
    and single/double quotes around the value are stripped. Malformed lines are
    skipped rather than failing the run.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_api_key(
    provider: str,
    environ: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> str:
    """Return the API key for *provider*, or raise :class:`ApiKeyError`.

    The process environment wins over the orchestrator-root ``.env`` file.
    """
    key_vars = _PROVIDER_KEY_VARS.get(provider)
    if key_vars is None:
        raise ValueError(f"Unknown agent provider for API-key auth: {provider}")
    env = os.environ if environ is None else environ
    for name in key_vars:
        value = env.get(name, "").strip()
        if value:
            return value
    file_path = env_file if env_file is not None else _orchestrator_env_file()
    file_values = load_env_file(file_path)
    for name in key_vars:
        value = file_values.get(name, "").strip()
        if value:
            return value
    raise ApiKeyError(
        f"Backend 'api' for agent '{provider}' needs an API key, but none of "
        f"{', '.join(key_vars)} is set in the environment or in {file_path}. "
        "Export the variable or add it to the orchestrator .env file "
        "(see .env.example), or use backend 'cli' for subscription auth."
    )


def subprocess_env(
    provider: str,
    backend: str,
    environ: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> dict[str, str]:
    """Build the environment for the provider CLI subprocess.

    ``cli`` strips the provider's API-key variables (deterministic subscription
    billing); ``api`` injects the resolved key into every variable name the CLI
    may read. The rest of the environment is inherited unchanged.
    """
    strip_vars = _PROVIDER_STRIP_VARS.get(provider)
    if strip_vars is None:
        raise ValueError(f"Unknown agent provider for API-key auth: {provider}")
    if backend not in {"cli", "api"}:
        raise ValueError("backend must be 'cli' or 'api'")
    env = dict(os.environ if environ is None else environ)
    for name in strip_vars:
        env.pop(name, None)
    if backend == "api":
        key = resolve_api_key(provider, environ=environ, env_file=env_file)
        for name in _PROVIDER_KEY_VARS[provider]:
            env[name] = key
    return env
