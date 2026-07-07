"""Reusable run-file specification for the orchestration loop.

A run file is a small YAML document that captures every parameter needed to
launch a single orchestration run. It exists so repeated agent loops can be
started with one short command instead of a long argument list.

Path handling: relative paths inside a run file (``repo_path``, ``config``,
``task_file``) are resolved relative to the current working directory from which
the command is launched, not relative to the run-file location. This keeps the
behavior identical to passing the same paths directly on the command line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.agent_options import VALID_AGENT_PROVIDERS, VALID_BACKENDS


_BOOLEAN_FIELDS = ("use_worktree", "plan_only", "setup_only", "dry_run", "allow_dirty")
_BRANCH_MODES = ("worktree", "in_place", "none")
_CREATE_BRANCH_MODES = ("auto", "always", "never")
_KNOWN_FIELDS = frozenset(
    {
        "repo_path",
        "config",
        "agent",
        "backend",
        "use_worktree",
        "branch_mode",
        "create_branch",
        "allow_dirty",
        "base_branch",
        "agent_branch",
        "plan_only",
        "setup_only",
        "dry_run",
        "task",
        "task_file",
    }
)


@dataclass(frozen=True)
class RunFileConfig:
    """Parsed run-file parameters for a single orchestration run."""

    repo_path: Path | None
    config: Path | None
    agent: str
    backend: str
    use_worktree: bool
    branch_mode: str | None
    create_branch: str | None
    allow_dirty: bool
    base_branch: str | None
    agent_branch: str | None
    plan_only: bool
    setup_only: bool
    dry_run: bool
    task: str | None
    task_file: Path | None


def _resolve_path(value: str) -> Path:
    """Resolve a run-file path relative to the current working directory."""
    return Path(value).expanduser().resolve()


def _optional_string(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Run file '{key}' must be a string or null")
    return value


def load_run_file(path: Path) -> RunFileConfig:
    """Load and validate a run file into a :class:`RunFileConfig`.

    Validation rules:

    * ``repo_path`` is optional; when omitted the caller defaults it (to an
      explicit ``--repo-path`` or the current working directory);
    * either ``task`` or ``task_file`` is required, and they are mutually
      exclusive;
    * ``agent`` defaults to ``claude`` and must be ``claude`` or ``codex``;
    * ``backend`` defaults to ``cli`` and must be ``cli``;
    * boolean fields default to ``False``;
    * ``config`` is optional, so target-local config discovery still applies
      when it is omitted.
    """
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Run file not found: {resolved}")
    with resolved.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        raise ValueError(f"Run file is empty: {resolved}")
    return parse_run_data(data)


def parse_run_data(data: object) -> RunFileConfig:
    """Validate an already-loaded run-parameter mapping.

    Shared by run files and queue task files, which carry the same run
    parameters plus queue-only metadata that the queue strips before calling
    this.
    """
    if not isinstance(data, dict):
        raise ValueError("Run file root must be a YAML mapping")

    unknown = set(data) - _KNOWN_FIELDS
    if unknown:
        raise ValueError(f"Unknown run-file fields: {', '.join(sorted(unknown))}")

    repo_path_value = _optional_string(data, "repo_path")
    if repo_path_value is not None and not repo_path_value.strip():
        raise ValueError("Run file 'repo_path' must be a non-empty string when present")

    task_value = _optional_string(data, "task")
    task_file_value = _optional_string(data, "task_file")
    has_task = bool(task_value and task_value.strip())
    has_task_file = bool(task_file_value and task_file_value.strip())
    if has_task and has_task_file:
        raise ValueError("Run file 'task' and 'task_file' are mutually exclusive")
    if not has_task and not has_task_file:
        raise ValueError("Run file must define either 'task' or 'task_file'")

    agent_value = data.get("agent", "claude")
    if agent_value not in VALID_AGENT_PROVIDERS:
        raise ValueError("Run file 'agent' must be 'claude' or 'codex'")

    backend_value = data.get("backend", "cli")
    if backend_value not in VALID_BACKENDS:
        raise ValueError("Run file 'backend' must be 'cli'")

    booleans: dict[str, bool] = {}
    for field in _BOOLEAN_FIELDS:
        value = data.get(field, False)
        if not isinstance(value, bool):
            raise ValueError(f"Run file '{field}' must be a boolean")
        booleans[field] = value

    branch_mode_value = _optional_string(data, "branch_mode")
    if branch_mode_value is not None and branch_mode_value not in _BRANCH_MODES:
        raise ValueError(f"Run file 'branch_mode' must be one of: {', '.join(_BRANCH_MODES)}")
    create_branch_value = _optional_string(data, "create_branch")
    if create_branch_value is not None and create_branch_value not in _CREATE_BRANCH_MODES:
        raise ValueError(
            f"Run file 'create_branch' must be one of: {', '.join(_CREATE_BRANCH_MODES)}"
        )

    config_value = _optional_string(data, "config")
    base_branch_value = _optional_string(data, "base_branch")
    agent_branch_value = _optional_string(data, "agent_branch")

    return RunFileConfig(
        repo_path=_resolve_path(repo_path_value) if repo_path_value else None,
        config=_resolve_path(config_value) if config_value else None,
        agent=agent_value,
        backend=backend_value,
        use_worktree=booleans["use_worktree"],
        branch_mode=branch_mode_value,
        create_branch=create_branch_value,
        allow_dirty=booleans["allow_dirty"],
        base_branch=base_branch_value,
        agent_branch=agent_branch_value,
        plan_only=booleans["plan_only"],
        setup_only=booleans["setup_only"],
        dry_run=booleans["dry_run"],
        task=task_value if has_task else None,
        task_file=_resolve_path(task_file_value) if has_task_file else None,
    )


def resolve_task_text(run: RunFileConfig) -> str:
    """Return the task description, reading ``task_file`` when needed."""
    if run.task is not None:
        return run.task
    assert run.task_file is not None  # guaranteed by load_run_file validation
    try:
        return run.task_file.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not read task file {run.task_file}: {error}") from error
