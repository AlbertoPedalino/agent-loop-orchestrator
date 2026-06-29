"""Safe task -> plan -> implement -> verify -> fix -> review orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import time

import yaml

from agent.agent_options import VALID_AGENT_PROVIDERS, VALID_BACKENDS
from agent.claude_runner import run_claude_prompt
from agent.codex_runner import run_codex_prompt
from agent.log import get_logger
from agent.git_utils import (
    branch_exists,
    checkout_in_place_agent_branch,
    create_worktree,
    ensure_git_repo,
    fetch_remote,
    find_worktree_for_branch,
    get_current_branch,
    get_git_diff,
    get_git_status,
    is_protected_branch,
    remote_branch_exists,
)
from agent.hooks import post_phase_hook, pre_phase_hook
from agent.memory import (
    MEMORY_UPDATE_INSTRUCTION,
    MemoryConfig,
    format_memory_section,
    load_memory,
    resolve_memory_config,
    update_memory_from_output,
)
from agent.permissions import resolve_phase_permissions
from agent.report import write_report
from agent.subagents import SubagentConfig, load_subagents_config
from agent.verifier import run_verification_commands, verification_passed


PIPELINE_STEPS = (
    "target repository validation",
    "optional local worktree setup",
    "planner",
    "implementer",
    "verification",
    "fixer loop",
    "reviewer",
    "report",
)

logger = get_logger()


@dataclass(frozen=True)
class OrchestrationResult:
    """Artifacts and target context produced by an orchestration run."""

    run_dir: Path
    report_path: Path
    status: str
    target_repo_path: Path
    worktree_path: Path | None = None


@dataclass(frozen=True)
class ConfigSelection:
    """A resolved configuration path and the way it was selected."""

    path: Path
    source: str


def resolve_config_selection(
    repo_path: Path,
    explicit_config_path: Path | None = None,
    fallback_config_path: Path | None = None,
) -> ConfigSelection:
    """Select explicit, target-local, or generic fallback configuration.

    Target-local settings allow the orchestrator to stay project-agnostic while
    keeping repository rules beside the repository they govern.
    """
    if explicit_config_path is not None:
        return ConfigSelection(
            path=explicit_config_path.expanduser().resolve(),
            source="explicit --config",
        )

    resolved_repo_path = repo_path.expanduser().resolve()
    for relative_path, source in (
        (Path(".agent-loop/config.yaml"), "target-local .agent-loop/config.yaml"),
        (Path(".agent-loop.yaml"), "target-local .agent-loop.yaml"),
    ):
        candidate = resolved_repo_path / relative_path
        if candidate.is_file():
            return ConfigSelection(path=candidate, source=source)

    default_path = fallback_config_path or Path(__file__).resolve().parent.parent / "configs" / "default.yaml"
    return ConfigSelection(
        path=default_path.expanduser().resolve(),
        source="fallback orchestrator configs/default.yaml",
    )


def load_config(config_path: Path) -> dict[str, Any]:
    """Load a YAML configuration file as a mapping."""
    resolved_path = config_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {resolved_path}")
    with resolved_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}
    if not isinstance(config, dict):
        raise ValueError("Configuration root must be a YAML mapping")
    return config


def _mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.setdefault(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Configuration '{key}' must be a YAML mapping")
    return value


def _resolve_agent_provider(config: dict[str, Any], override: str | None) -> str:
    if override is not None:
        return override
    value = config.get("agent")
    if value is None:
        return "claude"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        provider = value.get("provider", "claude")
        if not isinstance(provider, str):
            raise ValueError("Configuration 'agent.provider' must be a string")
        return provider
    raise ValueError("Configuration 'agent' must be a string or YAML mapping")


def _create_run_dir(runs_root: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = runs_root / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_markdown(path: Path, content: str) -> None:
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _write_verification(path: Path, results: dict[str, str]) -> None:
    sections = ["# Verification Results"]
    if not results:
        sections.append("No verification commands were configured.")
    for command, output in results.items():
        sections.extend([f"## `{command}`", "```text", output.rstrip(), "```"])
    _write_markdown(path, "\n\n".join(sections))


def _resolve_project_context(config: dict[str, Any]) -> dict[str, Any]:
    """Return an optional project context mapping with basic shape validation."""
    project_context = config.get("project_context")
    if project_context is None:
        return {}
    if not isinstance(project_context, dict):
        raise ValueError("Configuration project_context must be a YAML mapping")
    for key in ("data_sources", "allowed_sources", "rules"):
        if key not in project_context:
            continue
        value = project_context[key]
        if key == "data_sources" and not isinstance(value, dict):
            raise ValueError("project_context.data_sources must be a YAML mapping")
        if key in {"allowed_sources", "rules"} and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"project_context.{key} must be a list of strings")
    return project_context


def _format_project_context(project_context: dict[str, Any]) -> str:
    """Format optional project rules as a stable, readable prompt section."""
    if not project_context:
        return ""

    lines = ["# Persistent Project Context", "", "These rules apply to every task for this project:"]
    data_sources = project_context.get("data_sources", {})
    if data_sources:
        lines.extend(["", "## Data Sources"])
        lines.extend(f"- **{key}**: {value}" for key, value in data_sources.items())
    allowed_sources = project_context.get("allowed_sources", [])
    if allowed_sources:
        lines.extend(["", "## Allowed Source Codes", "", f"- {', '.join(allowed_sources)}"])
    rules = project_context.get("rules", [])
    if rules:
        lines.extend(["", "## Rules"])
        lines.extend(f"- {rule}" for rule in rules)
    return "\n".join(lines)


def _read_prompt(
    subagent: SubagentConfig,
    task: str,
    repo_path: Path,
    context: str = "",
    project_context: str = "",
    project_memory: str = "",
    request_memory_update: bool = False,
) -> str:
    template = subagent.prompt_template.read_text(encoding="utf-8").strip()
    return (
        f"{template}\n\n"
        "## Orchestrator Context\n\n"
        f"Phase: {subagent.name}\n"
        f"Repository: {repo_path}\n\n"
        f"Task:\n{task.strip()}\n"
        + (f"\n{project_context.strip()}\n" if project_context.strip() else "")
        + (f"\n{project_memory.strip()}\n" if project_memory.strip() else "")
        + "\nSafety constraints: do not commit, push, switch to a protected branch, "
        "run blocked commands, or change files outside the requested scope.\n"
        + (f"\n{context.strip()}\n" if context.strip() else "")
        + (f"\n{MEMORY_UPDATE_INSTRUCTION}" if request_memory_update else "")
    )


def _append_phase_event(run_dir: Path | None, event: dict[str, Any]) -> None:
    """Append one phase guardrail event to the run's phase-events log.

    This is what gives the pre/post-phase hooks an observable effect: their
    deterministic metadata, the resolved agent/backend, and the enforced tool policy
    are recorded per run for auditing instead of being discarded.
    """
    if run_dir is None:
        return
    with (run_dir / "phase_events.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, sort_keys=True) + "\n")


def _run_phase(
    *,
    phase: str,
    prompt: str,
    task: str,
    repo_path: Path,
    agent: str,
    backend: str,
    subagent: SubagentConfig,
    max_budget_usd: float | None,
    blocked_commands: list[str] | None = None,
    timeout_seconds: int = 600,
    stream: bool = True,
    run_dir: Path | None = None,
) -> str:
    """Dispatch a configured phase through the selected agent/backend.

    Read-only phases (no write-capable tools) may run on a protected branch
    because their tool policy prevents any modification. Write-capable phases are
    refused on a protected branch and must never leave the repository on one.
    """
    permissions = resolve_phase_permissions(
        subagent.allowed_tools, subagent.permission_mode, blocked_commands or []
    )
    current_branch = get_current_branch(repo_path)
    if not subagent.is_read_only and is_protected_branch(current_branch):
        raise RuntimeError(
            f"Refusing to run write-capable phase '{phase}' on protected branch "
            f"'{current_branch}'. Use an isolated agent worktree or in-place agent branch."
        )
    pre_metadata = pre_phase_hook(phase, repo_path, task)
    selected_agent = subagent.agent or agent
    selected_backend = subagent.backend or backend
    logger.info(
        "▶ %s starting (agent=%s, backend=%s, %s, tools=%s)",
        phase,
        selected_agent,
        selected_backend,
        "read-only" if subagent.is_read_only else f"write/{permissions.permission_mode or 'default'}",
        ",".join(permissions.allowed_tools) or "none",
    )
    started_at = time.monotonic()
    if selected_agent == "claude" and selected_backend == "cli":
        output = run_claude_prompt(
            prompt,
            repo_path,
            allowed_tools=permissions.allowed_tools,
            disallowed_tools=permissions.disallowed_tools,
            permission_mode=permissions.permission_mode,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
            stream=stream,
            phase=phase,
        )
    elif selected_agent == "codex" and selected_backend == "cli":
        output = run_codex_prompt(
            prompt,
            repo_path,
            allowed_tools=permissions.allowed_tools,
            disallowed_tools=permissions.disallowed_tools,
            permission_mode=permissions.permission_mode,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
            stream=stream,
            phase=phase,
        )
    elif selected_agent == "codex":
        raise ValueError("Codex agent supports only backend 'cli'")
    else:
        raise ValueError(f"Unsupported agent/backend: {selected_agent}/{selected_backend}")
    logger.info(
        "✔ %s finished in %.1fs (%d chars)", phase, time.monotonic() - started_at, len(output)
    )
    if not subagent.is_read_only:
        branch_after_phase = get_current_branch(repo_path)
        if is_protected_branch(branch_after_phase):
            raise RuntimeError(
                f"Phase '{phase}' left the repository on protected branch "
                f"'{branch_after_phase}'."
            )
    post_metadata = post_phase_hook(phase, output)
    _append_phase_event(
        run_dir,
        {
            **pre_metadata,
            **post_metadata,
            "agent": selected_agent,
            "backend": selected_backend,
            "read_only": subagent.is_read_only,
            "permission_mode": permissions.permission_mode or "default",
            "allowed_tools": permissions.allowed_tools,
            "disallowed_tools": permissions.disallowed_tools,
        },
    )
    return output


def _dry_run_output(phase: str) -> str:
    return (
        f"# Dry-run {phase.title()}\n\n"
        "No agent was invoked. This phase is simulated and no target repository files were modified."
    )


def _worktree_root(value: str | Path, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root / path).resolve()


def _resolve_or_create_worktree(
    *,
    repo_path: Path,
    worktree_root: Path,
    agent_branch: str,
    base_branch: str,
    remote: str,
) -> tuple[Path, bool]:
    """Reuse a registered agent worktree or safely create a new one.

    The boolean result is ``True`` when an existing worktree was reused. This
    common path is used by setup-only, plan-only, and normal full runs.
    """
    existing_worktree = find_worktree_for_branch(repo_path, agent_branch)
    if existing_worktree is not None:
        if not existing_worktree.is_dir():
            raise FileNotFoundError(
                f"Registered worktree path is unavailable: {existing_worktree}"
            )
        return existing_worktree, True

    base_is_available = branch_exists(repo_path, base_branch) or remote_branch_exists(
        repo_path, remote, base_branch
    )
    if not base_is_available:
        fetch_remote(repo_path, remote)
    return (
        create_worktree(repo_path, worktree_root, agent_branch, base_branch, remote),
        False,
    )


def _resolve_branch_mode(branch_mode: str | None, use_worktree: bool) -> tuple[str, bool]:
    """Resolve the effective branch mode and whether a worktree is used.

    ``use_worktree: true`` implies ``branch_mode: worktree`` and takes precedence,
    so an explicit worktree request is never silently downgraded. ``branch_mode:
    worktree`` likewise turns the worktree path on.
    """
    if branch_mode is not None and branch_mode not in {"worktree", "in_place", "none"}:
        raise ValueError("branch_mode must be 'worktree', 'in_place', or 'none'")
    if use_worktree or branch_mode == "worktree":
        return "worktree", True
    return (branch_mode or "none"), False


def _wants_in_place_branch(create_branch: str, plan_only: bool, setup_only: bool) -> bool:
    """Decide whether an in-place run should create/checkout the agent branch.

    Plan-only never creates a branch. ``create_branch: never`` never creates one;
    ``always`` always does. Under ``auto`` a setup-only run does not create a
    branch (it must be requested explicitly), while a full loop does.
    """
    if plan_only:
        return False
    if create_branch == "never":
        return False
    if create_branch == "always":
        return True
    return not setup_only


def _record_memory_update(memory_config: MemoryConfig, output: str) -> str:
    """Persist a memory block from *output* and return a report-friendly status."""
    if not memory_config.enabled:
        return "disabled"
    if update_memory_from_output(memory_config, output):
        return f"updated ({memory_config.path})"
    return "unchanged (no memory block emitted)"


def _finalize_git_details(details: dict[str, str | int], repo_path: Path) -> None:
    """Record the final branch and end-of-run dirty state on every report path."""
    try:
        details["final branch"] = get_current_branch(repo_path)
    except Exception:  # noqa: BLE001 - reporting must never mask the real outcome
        details["final branch"] = "unknown"
    details["working tree dirty at end"] = "yes" if get_git_status(repo_path).strip() else "no"


def run_orchestrator(
    *,
    repo_path: Path,
    task: str,
    config_path: Path,
    config_source: str = "explicit configuration",
    max_fix_attempts: int | None = None,
    dry_run: bool = False,
    agent: str | None = None,
    backend: str | None = None,
    use_worktree: bool | None = None,
    branch_mode: str | None = None,
    create_branch: str | None = None,
    allow_dirty: bool | None = None,
    worktree_root: Path | None = None,
    base_branch: str | None = None,
    agent_branch: str | None = None,
    remote: str | None = None,
    subagents_config_path: Path | None = None,
    setup_only: bool = False,
    plan_only: bool = False,
    run_file_path: Path | None = None,
    launched_from: str = "cli-args",
    repo_path_source: str = "cli --repo-path",
    stream: bool = True,
) -> OrchestrationResult:
    """Run the safe orchestration loop, or simulate it in ``dry_run`` mode.

    A dry run writes only run metadata. It never calls an agent, runs verification,
    creates a worktree, or modifies the target repository. ``plan_only`` executes
    only the planner phase and then records read-only Git state.
    """
    project_root = Path(__file__).resolve().parent.parent
    resolved_repo_path = repo_path.expanduser().resolve()
    if not resolved_repo_path.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {resolved_repo_path}")
    if not ensure_git_repo(resolved_repo_path):
        raise ValueError(f"Repository path is not a Git work tree: {resolved_repo_path}")
    if not task.strip():
        raise ValueError("Task must not be empty")
    if setup_only and plan_only:
        raise ValueError("plan_only and setup_only cannot be used together")

    resolved_config_path = config_path.expanduser().resolve()
    config = deepcopy(load_config(resolved_config_path))
    project_context = _resolve_project_context(config)
    formatted_project_context = _format_project_context(project_context)
    # Memory persists in the main repository so it survives across runs/worktrees.
    memory_config = resolve_memory_config(config, resolved_repo_path)
    formatted_project_memory = format_memory_section(load_memory(memory_config))
    project_config = _mapping(config, "project")
    backend_config = _mapping(config, "backend")
    git_config = _mapping(config, "git")
    limits = _mapping(config, "limits")
    verification_config = _mapping(config, "verification")
    if max_fix_attempts is not None:
        if max_fix_attempts < 0:
            raise ValueError("max_fix_attempts must be zero or greater")
        limits["max_fix_attempts"] = max_fix_attempts

    selected_agent = _resolve_agent_provider(config, agent)
    if selected_agent not in VALID_AGENT_PROVIDERS:
        raise ValueError("Agent must be 'claude' or 'codex'")
    selected_backend = backend or backend_config.get("type", "cli")
    if selected_backend not in VALID_BACKENDS:
        raise ValueError("Backend must be 'cli'")
    selected_remote = remote or git_config.get("remote", "origin")
    if not isinstance(selected_remote, str) or not selected_remote.strip():
        raise ValueError("Git remote must be a non-empty string")
    selected_use_worktree = (
        use_worktree if use_worktree is not None else bool(project_config.get("use_worktree", False))
    )
    requested_branch_mode = branch_mode or git_config.get("branch_mode")
    selected_branch_mode, selected_use_worktree = _resolve_branch_mode(
        requested_branch_mode, selected_use_worktree
    )
    selected_create_branch = create_branch or git_config.get("create_branch") or "auto"
    if selected_create_branch not in {"auto", "always", "never"}:
        raise ValueError("create_branch must be 'auto', 'always', or 'never'")
    selected_allow_dirty = (
        allow_dirty if allow_dirty is not None else bool(git_config.get("allow_dirty", False))
    )
    selected_base_branch = base_branch or git_config.get("base_branch")
    selected_agent_branch = agent_branch or git_config.get("agent_branch")
    wants_in_place_branch = selected_branch_mode == "in_place" and _wants_in_place_branch(
        selected_create_branch, plan_only, setup_only
    )
    if selected_use_worktree:
        if not isinstance(selected_base_branch, str) or not selected_base_branch.strip():
            raise ValueError("A base branch is required when using a worktree")
        if not isinstance(selected_agent_branch, str) or not selected_agent_branch.strip():
            raise ValueError("An agent branch is required when using a worktree")
        if is_protected_branch(selected_agent_branch):
            raise ValueError(f"Refusing protected agent branch: {selected_agent_branch}")
    if wants_in_place_branch:
        if not isinstance(selected_agent_branch, str) or not selected_agent_branch.strip():
            raise ValueError("An agent branch is required for an in-place implementation loop")
        if not isinstance(selected_base_branch, str) or not selected_base_branch.strip():
            raise ValueError("A base branch is required for an in-place implementation loop")
        if is_protected_branch(selected_agent_branch):
            raise ValueError(f"Refusing protected agent branch: {selected_agent_branch}")
        if selected_agent_branch.strip() == selected_base_branch.strip():
            raise ValueError("Agent branch must differ from the base branch")
    if bool(git_config.get("require_clean_repo", False)) and get_git_status(resolved_repo_path).strip():
        raise ValueError("Target repository must be clean according to git.require_clean_repo")

    raw_commands = verification_config.get("commands", [])
    if not isinstance(raw_commands, list) or not all(isinstance(command, str) for command in raw_commands):
        raise ValueError("verification.commands must be a list of strings")
    blocked_commands = config.get("blocked_commands", [])
    if not isinstance(blocked_commands, list) or not all(
        isinstance(command, str) for command in blocked_commands
    ):
        raise ValueError("blocked_commands must be a list of strings")
    timeout_seconds = verification_config.get("timeout_seconds", 120)
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ValueError("verification.timeout_seconds must be a positive integer")
    max_attempts = limits.get("max_fix_attempts", 3)
    if not isinstance(max_attempts, int) or max_attempts < 0:
        raise ValueError("limits.max_fix_attempts must be a non-negative integer")
    max_budget = limits.get("max_budget_usd")
    if max_budget is not None and not isinstance(max_budget, (int, float)):
        raise ValueError("limits.max_budget_usd must be numeric")
    if max_budget is not None:
        raise ValueError("limits.max_budget_usd is not supported in subscription CLI mode")
    max_changed_files = limits.get("max_changed_files", 8)
    if not isinstance(max_changed_files, int) or max_changed_files <= 0:
        raise ValueError("limits.max_changed_files must be a positive integer")
    phase_timeout_seconds = limits.get("phase_timeout_seconds", 600)
    if not isinstance(phase_timeout_seconds, int) or phase_timeout_seconds <= 0:
        raise ValueError("limits.phase_timeout_seconds must be a positive integer")

    subagents_path = subagents_config_path or project_root / "configs" / "subagents.default.yaml"
    subagents = load_subagents_config(subagents_path)
    required_subagents = ("planner", "implementer", "fixer", "reviewer")
    missing_subagents = [name for name in required_subagents if name not in subagents]
    if missing_subagents:
        raise ValueError(f"Missing required subagent configurations: {', '.join(missing_subagents)}")

    run_dir = _create_run_dir(project_root / "runs")
    _write_markdown(run_dir / "task.md", f"# Task\n\n{task.strip()}")
    (run_dir / "config_snapshot.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    _write_markdown(
        run_dir / "config_source.md",
        "# Config Source\n\n"
        f"- **Path**: {resolved_config_path}\n"
        f"- **Selection**: {config_source}",
    )
    run_source_lines = [
        "# Run Source",
        "",
        f"- **Launched from**: {launched_from}",
        f"- **Resolved repo path**: {resolved_repo_path}",
        f"- **Repo path source**: {repo_path_source}",
    ]
    if run_file_path is not None:
        run_source_lines.append(f"- **Run file**: {run_file_path}")
    _write_markdown(run_dir / "run_source.md", "\n".join(run_source_lines))
    if formatted_project_context:
        _write_markdown(run_dir / "project_context.md", formatted_project_context)
    _write_markdown(run_dir / "pipeline.md", "# Intended Pipeline\n\n" + "\n".join(
        f"{index}. {step}" for index, step in enumerate(PIPELINE_STEPS, start=1)
    ))

    active_repo_path = resolved_repo_path
    worktree_path: Path | None = None
    current_branch = get_current_branch(resolved_repo_path)
    original_branch = current_branch
    branch_created = False
    branch_reused = False
    in_place_note = "not requested"
    worktree_note = "not requested"
    if selected_use_worktree:
        raw_worktree_root = worktree_root or git_config.get("worktree_root", "../agent-worktrees")
        selected_worktree_root = _worktree_root(raw_worktree_root, project_root)
        worktree_note = (
            f"branch {selected_agent_branch} from {selected_remote}/{selected_base_branch} at "
            f"{selected_worktree_root}"
        )
        if dry_run:
            existing_worktree = find_worktree_for_branch(
                resolved_repo_path, selected_agent_branch
            )
            if existing_worktree is not None:
                worktree_note = f"would reuse existing worktree at {existing_worktree}"
                logger.info("Dry run: %s.", worktree_note)
            else:
                logger.info("Dry run: would create local worktree for %s.", worktree_note)
        else:
            worktree_path, reused_worktree = _resolve_or_create_worktree(
                repo_path=resolved_repo_path,
                worktree_root=selected_worktree_root,
                agent_branch=selected_agent_branch,
                base_branch=selected_base_branch,
                remote=selected_remote,
            )
            if reused_worktree:
                worktree_note = f"using existing worktree at {worktree_path}"
            active_repo_path = worktree_path
            current_branch = get_current_branch(active_repo_path)
    elif wants_in_place_branch:
        in_place_note = f"branch {selected_agent_branch} from {selected_base_branch} (in place)"
        if dry_run:
            logger.info("Dry run: would create/checkout in-place %s.", in_place_note)
        else:
            branch_result = checkout_in_place_agent_branch(
                resolved_repo_path,
                selected_agent_branch,
                selected_base_branch,
                remote=selected_remote,
                allow_dirty=selected_allow_dirty,
            )
            branch_created = branch_result.created
            branch_reused = branch_result.reused
            current_branch = get_current_branch(resolved_repo_path)
            in_place_note = (
                f"{'created' if branch_created else 'reused'} branch "
                f"{branch_result.final_branch} in {resolved_repo_path}"
            )

    logger.info("Prepared pipeline: %s", " -> ".join(PIPELINE_STEPS))
    logger.info("Target repository: %s", active_repo_path)
    logger.info("Agent: %s, backend: %s (stream=%s)", selected_agent, selected_backend, stream)
    if dry_run:
        logger.info(
            "Dry run: agent execution, verification, worktree creation, and target changes are disabled."
        )

    details: dict[str, str | int] = {
        "agent": selected_agent,
        "backend": selected_backend,
        "launched from": launched_from,
        "run file": str(run_file_path) if run_file_path is not None else "not used",
        "original repo path": str(repo_path),
        "resolved repo path": str(resolved_repo_path),
        "repo path source": repo_path_source,
        "config path": str(resolved_config_path),
        "config source": config_source,
        "target repository": str(active_repo_path),
        "branch mode": selected_branch_mode,
        "create branch mode": selected_create_branch,
        "original branch": original_branch,
        "base branch": selected_base_branch if selected_base_branch else "not set",
        "agent branch": selected_agent_branch if selected_agent_branch else "not set",
        "branch created": "yes" if branch_created else "no",
        "branch reused": "yes" if branch_reused else "no",
        "in-place branch": in_place_note,
        "starting branch": current_branch,
        "worktree": str(worktree_path) if worktree_path else worktree_note,
        "verification": "not run",
        "fix attempts": 0,
        "changed files": 0,
        "next steps": "Review the saved phase outputs before allowing any follow-up action.",
    }

    try:
        if setup_only:
            status = "dry-run-setup" if dry_run else "setup-complete"
            details["next steps"] = "Run a planner phase separately; no agent phase was invoked."
            _finalize_git_details(details, active_repo_path)
            report_path = write_report(run_dir, task, status, details)
            return OrchestrationResult(
                run_dir, report_path, status, active_repo_path, worktree_path
            )

        if dry_run:
            _write_markdown(run_dir / "planner_output.md", _dry_run_output("planner"))
            if plan_only:
                planner_prompt = _read_prompt(
                    subagents["planner"],
                    task,
                    active_repo_path,
                    project_context=formatted_project_context,
                    project_memory=formatted_project_memory,
                    request_memory_update=memory_config.enabled,
                )
                _write_markdown(run_dir / "planner_prompt.md", planner_prompt)
                dry_run_status = get_git_status(active_repo_path)
                dry_run_diff = get_git_diff(active_repo_path)
                _write_markdown(run_dir / "git_status.txt", dry_run_status or "Working tree clean.")
                _write_markdown(run_dir / "git_diff.patch", dry_run_diff or "# No Diff\n")
            else:
                for phase in ("implementer", "reviewer"):
                    _write_markdown(run_dir / f"{phase}_output.md", _dry_run_output(phase))
                _write_markdown(
                    run_dir / "diff_after_implementer.patch",
                    "# Dry-run Diff\n\nNo target repository diff was collected or created.",
                )
                _write_verification(
                    run_dir / "verification_attempt_1.txt",
                    {"dry-run": "exit code: skipped\nstdout:\n\nstderr:\nVerification was not run."},
                )
            details["verification"] = "skipped (dry run)"
            details["risks"] = "No agent or verification command was executed."
            details["mode"] = "plan-only" if plan_only else "full-pipeline"
            status = "dry-run-plan-only" if plan_only else "dry-run"
        else:
            planner_prompt = _read_prompt(
                subagents["planner"],
                task,
                active_repo_path,
                project_context=formatted_project_context,
                project_memory=formatted_project_memory,
                request_memory_update=memory_config.enabled and plan_only,
            )
            _write_markdown(run_dir / "planner_prompt.md", planner_prompt)
            planner_output = _run_phase(
                phase="planner",
                prompt=planner_prompt,
                task=task,
                repo_path=active_repo_path,
                agent=selected_agent,
                backend=selected_backend,
                subagent=subagents["planner"],
                max_budget_usd=float(max_budget) if max_budget is not None else None,
                blocked_commands=blocked_commands,
                timeout_seconds=phase_timeout_seconds,
                stream=stream,
                run_dir=run_dir,
            )
            _write_markdown(run_dir / "planner_output.md", planner_output)

            if plan_only:
                planner_status = get_git_status(active_repo_path)
                planner_diff = get_git_diff(active_repo_path)
                _write_markdown(run_dir / "git_status.txt", planner_status or "Working tree clean.")
                _write_markdown(run_dir / "git_diff.patch", planner_diff or "# No Diff\n")
                details["verification"] = "not run (plan-only)"
                details["git status"] = planner_status or "working tree clean"
                details["changed files"] = len(
                    [line for line in planner_status.splitlines() if line]
                )
                details["diff lines"] = len(planner_diff.splitlines())
                details["mode"] = "plan-only"
                details["risks"] = "Planner output is advisory; review it before any implementation."
                details["next steps"] = "Review planner_output.md before allowing implementation."
                details["memory"] = _record_memory_update(memory_config, planner_output)
                status = "plan-only-complete"
                _finalize_git_details(details, active_repo_path)
                report_path = write_report(run_dir, task, status, details)
                return OrchestrationResult(
                    run_dir, report_path, status, active_repo_path, worktree_path
                )

            implementer_output = _run_phase(
                phase="implementer",
                prompt=_read_prompt(
                    subagents["implementer"],
                    task,
                    active_repo_path,
                    f"## Planner Output\n\n{planner_output}",
                    formatted_project_context,
                    formatted_project_memory,
                ),
                task=task,
                repo_path=active_repo_path,
                agent=selected_agent,
                backend=selected_backend,
                subagent=subagents["implementer"],
                max_budget_usd=float(max_budget) if max_budget is not None else None,
                blocked_commands=blocked_commands,
                timeout_seconds=phase_timeout_seconds,
                stream=stream,
                run_dir=run_dir,
            )
            _write_markdown(run_dir / "implementer_output.md", implementer_output)
            diff = get_git_diff(active_repo_path)
            _write_markdown(run_dir / "diff_after_implementer.patch", diff or "# No Diff\n")
            changed_after_implementer = [
                line for line in get_git_status(active_repo_path).splitlines() if line
            ]
            if len(changed_after_implementer) > max_changed_files:
                raise RuntimeError(
                    f"Changed-file limit exceeded: {len(changed_after_implementer)} > {max_changed_files}"
                )

            verification_results = run_verification_commands(
                active_repo_path, raw_commands, blocked_commands, timeout_seconds
            )
            _write_verification(run_dir / "verification_attempt_1.txt", verification_results)
            passed = verification_passed(verification_results)
            fix_attempts = 0
            while not passed and fix_attempts < max_attempts:
                fix_attempts += 1
                verification_context = "\n\n".join(
                    f"### {command}\n{output}" for command, output in verification_results.items()
                )
                fixer_output = _run_phase(
                    phase="fixer",
                    prompt=_read_prompt(
                        subagents["fixer"],
                        task,
                        active_repo_path,
                        "## Planner Output\n\n"
                        f"{planner_output}\n\n## Current Diff\n\n{get_git_diff(active_repo_path)}\n\n"
                        f"## Verification Attempt {fix_attempts}\n\n{verification_context}",
                        formatted_project_context,
                        formatted_project_memory,
                    ),
                    task=task,
                    repo_path=active_repo_path,
                    agent=selected_agent,
                    backend=selected_backend,
                    subagent=subagents["fixer"],
                    max_budget_usd=float(max_budget) if max_budget is not None else None,
                    blocked_commands=blocked_commands,
                    timeout_seconds=phase_timeout_seconds,
                    stream=stream,
                    run_dir=run_dir,
                )
                _write_markdown(run_dir / f"fixer_output_attempt_{fix_attempts}.md", fixer_output)
                verification_results = run_verification_commands(
                    active_repo_path, raw_commands, blocked_commands, timeout_seconds
                )
                _write_verification(
                    run_dir / f"verification_attempt_{fix_attempts + 1}.txt", verification_results
                )
                passed = verification_passed(verification_results)

            final_diff = get_git_diff(active_repo_path)
            _write_markdown(run_dir / "diff_after_fixes.patch", final_diff or "# No Diff\n")
            reviewer_output = _run_phase(
                phase="reviewer",
                prompt=_read_prompt(
                    subagents["reviewer"],
                    task,
                    active_repo_path,
                    f"## Final Diff\n\n{final_diff}\n\n"
                    f"## Verification Status\n\n{'passed' if passed else 'failed after fix limit'}",
                    formatted_project_context,
                    formatted_project_memory,
                    request_memory_update=memory_config.enabled,
                ),
                task=task,
                repo_path=active_repo_path,
                agent=selected_agent,
                backend=selected_backend,
                subagent=subagents["reviewer"],
                max_budget_usd=float(max_budget) if max_budget is not None else None,
                blocked_commands=blocked_commands,
                timeout_seconds=phase_timeout_seconds,
                stream=stream,
                run_dir=run_dir,
            )
            _write_markdown(run_dir / "reviewer_output.md", reviewer_output)

            status_lines = [line for line in get_git_status(active_repo_path).splitlines() if line]
            details["verification"] = "passed" if passed else "failed after fix limit"
            details["fix attempts"] = fix_attempts
            details["changed files"] = len(status_lines)
            details["diff lines"] = len(final_diff.splitlines())
            details["risks"] = (
                "Verification remains failing; inspect fixer and reviewer output."
                if not passed
                else "Review the reviewer output before committing any changes."
            )
            details["memory"] = _record_memory_update(memory_config, reviewer_output)
            status = "completed" if passed else "verification-failed"

        _finalize_git_details(details, active_repo_path)
        report_path = write_report(run_dir, task, status, details)
        return OrchestrationResult(run_dir, report_path, status, active_repo_path, worktree_path)
    except Exception as error:
        details["failure"] = str(error)
        _finalize_git_details(details, active_repo_path)
        write_report(run_dir, task, "failed", details)
        raise
