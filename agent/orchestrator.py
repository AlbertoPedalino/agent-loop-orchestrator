"""Safe task -> plan -> implement -> verify -> fix -> review orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from fnmatch import fnmatchcase
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
    commit_all_changes,
    create_worktree,
    ensure_git_repo,
    fetch_remote,
    find_worktree_for_branch,
    get_current_branch,
    get_changed_paths,
    get_deleted_paths,
    get_git_diff,
    get_git_status,
    is_protected_branch,
    remote_branch_exists,
    remove_worktree,
)
from agent.hooks import (
    HooksConfig,
    load_hooks_config,
    post_phase_hook,
    pre_phase_hook,
    run_custom_hooks,
)
from agent.memory import (
    MEMORY_UPDATE_INSTRUCTION,
    MemoryConfig,
    append_history,
    format_history_section,
    format_memory_section,
    load_memory,
    load_recent_history,
    resolve_memory_config,
    update_memory_from_output,
)
from agent.permissions import resolve_phase_permissions
from agent.report import write_report
from agent.review_gate import (
    VERDICT_FILE_NAME,
    VERDICT_INSTRUCTION,
    ReviewVerdict,
    extract_verdict,
)
from agent.skills import (
    allowed_tools_with_skill,
    format_skills_system_prompt,
    inline_skills_for_codex,
)
from agent.subagents import (
    SubagentConfig,
    load_subagents_config,
    load_subagents_with_target_overlay,
)
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


def _resource_root() -> Path:
    """Return the installed package's immutable runtime-resource directory."""
    return Path(__file__).resolve().parent / "resources"


@dataclass(frozen=True)
class OrchestrationResult:
    """Artifacts and target context produced by an orchestration run."""

    run_dir: Path
    report_path: Path
    status: str
    target_repo_path: Path
    worktree_path: Path | None = None
    review_verdict: ReviewVerdict | None = None


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

    default_path = fallback_config_path or _resource_root() / "configs" / "default.yaml"
    return ConfigSelection(
        path=default_path.expanduser().resolve(),
        source="packaged fallback agent/resources/configs/default.yaml",
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


def _optional_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"Configuration '{key}' must be a boolean")
    return value


def _changed_files(repo_path: Path) -> list[str]:
    """Return every changed path, including files inside untracked directories."""
    return [line for line in get_git_status(repo_path).splitlines() if line]


def _enforce_changed_file_limit(repo_path: Path, maximum: int, phase: str) -> None:
    changed = _changed_files(repo_path)
    if len(changed) > maximum:
        raise RuntimeError(
            f"Changed-file limit exceeded after {phase}: {len(changed)} > {maximum}"
        )


def _result_status(passed: bool, review_verdict: ReviewVerdict | None) -> str:
    """Resolve the terminal status without hiding an actionable review verdict."""
    if not passed:
        return "verification-failed"
    if review_verdict is None or review_verdict.verdict == "approve":
        return "completed"
    return "review-rejected" if review_verdict.verdict == "reject" else "review-revise"


def _matches_any_path(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatchcase(normalized, pattern) for pattern in patterns)


def _enforce_testing_policy(
    repo_path: Path,
    *,
    policy: str,
    test_patterns: list[str],
    forbid_test_deletion: bool,
) -> list[str]:
    """Require a persistent test diff and reject deleted tests when configured."""
    deleted_tests = [
        path for path in get_deleted_paths(repo_path) if _matches_any_path(path, test_patterns)
    ]
    deleted_set = set(deleted_tests)
    changed_tests = [
        path
        for path in get_changed_paths(repo_path)
        if path not in deleted_set and _matches_any_path(path, test_patterns)
    ]
    if forbid_test_deletion and deleted_tests:
        raise RuntimeError("Test deletion is forbidden: " + ", ".join(deleted_tests))
    if policy == "required" and not changed_tests:
        raise RuntimeError(
            "Testing policy requires at least one added or modified test file matching: "
            + ", ".join(test_patterns)
        )
    return changed_tests


def _testing_prompt(policy: str, test_patterns: list[str], forbid_test_deletion: bool) -> str:
    if policy != "required" and not forbid_test_deletion:
        return ""
    lines = ["# Testing Policy", ""]
    if policy == "required":
        lines.append(
            "This task must add or modify at least one durable test matching: "
            + ", ".join(test_patterns)
            + "."
        )
    if forbid_test_deletion:
        lines.append("Do not delete test files.")
    lines.append("The full configured verification suite must pass before acceptance.")
    return "\n".join(lines)


def _checkpoint_message(task: str) -> str:
    summary = " ".join(task.split())
    if len(summary) > 72:
        summary = summary[:69].rstrip() + "..."
    return f"agent-loop: {summary}"


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
    """Create a unique run directory, disambiguating concurrent creations.

    Parallel queue workers can start runs in the same instant; a numeric suffix
    resolves the (already microsecond-rare) timestamp collision instead of
    failing the run.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for attempt in range(100):
        run_dir = runs_root / (timestamp if attempt == 0 else f"{timestamp}-{attempt}")
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return run_dir
    raise RuntimeError(f"Could not create a unique run directory under {runs_root}")


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
    request_verdict: bool = False,
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
        + (f"\n{VERDICT_INSTRUCTION}" if request_verdict else "")
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
    hooks_config: HooksConfig | None = None,
) -> str:
    """Dispatch a configured phase through the selected agent/backend.

    Read-only phases (no write-capable tools) may run on a protected branch
    because their tool policy prevents any modification. Write-capable phases are
    refused on a protected branch and must never leave the repository on one.

    Declared skills are policy, not payload: for the Claude backend the CLI
    discovers skill content natively, so the phase only gains the ``Skill`` tool
    plus a system-prompt instruction to invoke them. Codex has no skill loader,
    so repository-local skill bodies are inlined into the prompt instead.
    """
    selected_agent = subagent.agent or agent
    selected_backend = subagent.backend or backend
    phase_allowed_tools = subagent.allowed_tools
    skills_system_prompt: str | None = None
    if subagent.skills:
        if selected_agent == "claude":
            phase_allowed_tools = allowed_tools_with_skill(phase_allowed_tools)
            skills_system_prompt = format_skills_system_prompt(subagent.skills)
        else:
            prompt = inline_skills_for_codex(prompt, subagent.skills, repo_path)
    permissions = resolve_phase_permissions(
        phase_allowed_tools, subagent.permission_mode, blocked_commands or []
    )
    current_branch = get_current_branch(repo_path)
    if not subagent.is_read_only and is_protected_branch(current_branch):
        raise RuntimeError(
            f"Refusing to run write-capable phase '{phase}' on protected branch "
            f"'{current_branch}'. Use an isolated agent worktree or in-place agent branch."
        )
    if run_dir is not None and (subagent.skills or skills_system_prompt):
        # The pre-phase prompt artifacts are written before skill injection;
        # record what the backend actually received so skill delivery is auditable.
        sent = prompt if skills_system_prompt is None else (
            f"<!-- system prompt appended via CLI flag -->\n{skills_system_prompt}\n"
            f"<!-- end system prompt -->\n\n{prompt}"
        )
        (run_dir / f"{phase}_prompt_sent.md").write_text(sent, encoding="utf-8")
    pre_metadata = pre_phase_hook(phase, repo_path, task)
    logger.info(
        "▶ %s starting (agent=%s, backend=%s, %s, tools=%s, skills=%s)",
        phase,
        selected_agent,
        selected_backend,
        "read-only" if subagent.is_read_only else f"write/{permissions.permission_mode or 'default'}",
        ",".join(permissions.allowed_tools) or "none",
        ",".join(str(skill) for skill in subagent.skills) or "none",
    )
    started_at = time.monotonic()
    base_event: dict[str, Any] = {
        **pre_metadata,
        "agent": selected_agent,
        "backend": selected_backend,
        "read_only": subagent.is_read_only,
        "permission_mode": permissions.permission_mode or "default",
        "allowed_tools": permissions.allowed_tools,
        "disallowed_tools": permissions.disallowed_tools,
        "skills": [str(skill) for skill in subagent.skills],
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        # Custom gate: a configured pre_phase hook can veto the phase; the
        # rejection is recorded as a failed-phase event like any other error.
        run_custom_hooks(
            "pre_phase",
            hooks_config,
            repo_path,
            {"phase": phase, "agent": selected_agent, "backend": selected_backend},
        )
        if max_budget_usd is not None:
            if selected_backend != "api":
                raise ValueError(
                    f"limits.max_budget_usd requires backend 'api'; "
                    f"phase '{phase}' selected backend '{selected_backend}'."
                )
            if selected_agent != "claude":
                raise ValueError(
                    f"limits.max_budget_usd is supported only for Claude phases; "
                    f"phase '{phase}' selected agent '{selected_agent}'."
                )
        if selected_agent == "claude" and selected_backend in VALID_BACKENDS:
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
                append_system_prompt=skills_system_prompt,
                backend=selected_backend,
            )
        elif selected_agent == "codex" and selected_backend in VALID_BACKENDS:
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
                backend=selected_backend,
            )
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
    except Exception as error:
        # The failed phase is exactly the one an audit needs; record it before
        # propagating instead of leaving the events log without the failure.
        _append_phase_event(
            run_dir,
            {
                **base_event,
                "status": "failed",
                "error": str(error),
                "duration_seconds": round(time.monotonic() - started_at, 1),
            },
        )
        raise
    _append_phase_event(
        run_dir,
        {
            **base_event,
            **post_metadata,
            "duration_seconds": round(time.monotonic() - started_at, 1),
        },
    )
    run_custom_hooks(
        "post_phase",
        hooks_config,
        repo_path,
        {
            "phase": phase,
            "agent": selected_agent,
            "backend": selected_backend,
            "status": "completed",
            "output_length": str(len(output)),
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


def load_resumable_planner_output(run_dir: Path) -> str | None:
    """Return a previous run's planner output when it can seed a resumed run.

    A retry does not need to re-plan when the earlier attempt already produced a
    plan; the failure was in implementation or verification. Only a non-empty,
    non-dry-run planner output is resumable.
    """
    planner_path = run_dir / "planner_output.md"
    if not planner_path.is_file():
        return None
    content = planner_path.read_text(encoding="utf-8").strip()
    if not content or content.startswith("# Dry-run"):
        return None
    return content


def _record_run_history(
    memory_config: MemoryConfig,
    *,
    task: str,
    status: str,
    fix_attempts: int,
    run_dir: Path,
) -> None:
    """Append this run's outcome to the cross-run history log (best effort)."""
    try:
        append_history(
            memory_config,
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "task": " ".join(task.split())[:200],
                "status": status,
                "fix_attempts": fix_attempts,
                "run_dir": str(run_dir),
            },
        )
    except OSError as error:  # history must never mask the real outcome
        logger.warning("Could not append run history: %s", error)


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


def _cleanup_worktree_if_requested(
    *,
    details: dict[str, str | int],
    source_repo_path: Path,
    worktree_path: Path | None,
    created_for_run: bool,
    requested: bool,
) -> None:
    """Best-effort opt-in cleanup for clean worktrees created by this run."""
    if not requested:
        details["worktree cleanup"] = "not requested"
        return
    if worktree_path is None:
        details["worktree cleanup"] = "not applicable"
        return
    if not created_for_run:
        details["worktree cleanup"] = "skipped (worktree was reused)"
        return

    try:
        resolved_source = source_repo_path.expanduser().resolve()
        resolved_worktree = worktree_path.expanduser().resolve()
        if resolved_source == resolved_worktree:
            details["worktree cleanup"] = "skipped (source repository)"
            return
        if get_git_status(resolved_worktree).strip():
            details["worktree cleanup"] = "skipped (working tree dirty)"
            return
        remove_worktree(resolved_source, resolved_worktree)
    except Exception as error:  # noqa: BLE001 - cleanup must not mask run result
        logger.warning("Worktree cleanup failed for %s: %s", worktree_path, error)
        details["worktree cleanup"] = f"failed: {error}"
    else:
        details["worktree cleanup"] = f"removed {resolved_worktree}"


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
    resume_from_run_dir: Path | None = None,
) -> OrchestrationResult:
    """Run the safe orchestration loop, or simulate it in ``dry_run`` mode.

    A dry run writes only run metadata. It never calls an agent, runs verification,
    creates a worktree, or modifies the target repository. ``plan_only`` executes
    only the planner phase and then records read-only Git state.
    """
    project_root = Path(__file__).resolve().parent.parent
    resource_root = _resource_root()
    if resume_from_run_dir is not None and (plan_only or setup_only or dry_run):
        raise ValueError("resume_from_run_dir requires a full implementation loop")
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
    formatted_project_memory = "\n\n".join(
        section
        for section in (
            format_memory_section(load_memory(memory_config)),
            format_history_section(load_recent_history(memory_config)),
        )
        if section
    )
    project_config = _mapping(config, "project")
    backend_config = _mapping(config, "backend")
    git_config = _mapping(config, "git")
    limits = _mapping(config, "limits")
    testing_config = _mapping(config, "testing")
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
        raise ValueError("Backend must be 'cli' (subscription) or 'api' (API-key billing)")
    selected_remote = remote or git_config.get("remote", "origin")
    if not isinstance(selected_remote, str) or not selected_remote.strip():
        raise ValueError("Git remote must be a non-empty string")
    selected_use_worktree = (
        use_worktree
        if use_worktree is not None
        else _optional_bool(project_config, "use_worktree")
    )
    requested_branch_mode = branch_mode or git_config.get("branch_mode")
    selected_branch_mode, selected_use_worktree = _resolve_branch_mode(
        requested_branch_mode, selected_use_worktree
    )
    selected_create_branch = create_branch or git_config.get("create_branch") or "auto"
    if selected_create_branch not in {"auto", "always", "never"}:
        raise ValueError("create_branch must be 'auto', 'always', or 'never'")
    selected_allow_dirty = (
        allow_dirty if allow_dirty is not None else _optional_bool(git_config, "allow_dirty")
    )
    delete_worktree_on_success = _optional_bool(git_config, "delete_worktree_on_success")
    delete_worktree_on_failure = _optional_bool(git_config, "delete_worktree_on_failure")
    commit_on_success = _optional_bool(git_config, "commit_on_success")
    checkpoint_enabled_for_run = commit_on_success and not (
        plan_only or setup_only or dry_run
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
    if (
        checkpoint_enabled_for_run
        and not (selected_use_worktree or wants_in_place_branch)
    ):
        raise ValueError(
            "git.commit_on_success requires worktree mode or an in-place agent branch"
        )
    if _optional_bool(git_config, "require_clean_repo") and get_git_status(resolved_repo_path).strip():
        raise ValueError("Target repository must be clean according to git.require_clean_repo")

    raw_commands = verification_config.get("commands", [])
    if not isinstance(raw_commands, list) or not all(isinstance(command, str) for command in raw_commands):
        raise ValueError("verification.commands must be a list of strings")
    if checkpoint_enabled_for_run and not raw_commands:
        raise ValueError("git.commit_on_success needs at least one verification command")
    testing_policy = testing_config.get("policy", "optional")
    if testing_policy not in {"optional", "required"}:
        raise ValueError("testing.policy must be 'optional' or 'required'")
    test_patterns = testing_config.get(
        "paths", ["tests/**", "test_*.py", "**/test_*.py", "**/*_test.py"]
    )
    if not isinstance(test_patterns, list) or not test_patterns or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in test_patterns
    ):
        raise ValueError("testing.paths must be a non-empty list of strings")
    forbid_test_deletion = _optional_bool(testing_config, "forbid_test_deletion")
    if testing_policy == "required" and not raw_commands:
        raise ValueError("testing.policy 'required' needs at least one verification command")
    formatted_testing_policy = _testing_prompt(
        testing_policy, test_patterns, forbid_test_deletion
    )
    formatted_project_context = "\n\n".join(
        section
        for section in (formatted_project_context, formatted_testing_policy)
        if section
    )
    blocked_commands = config.get("blocked_commands", [])
    if not isinstance(blocked_commands, list) or not all(
        isinstance(command, str) for command in blocked_commands
    ):
        raise ValueError("blocked_commands must be a list of strings")
    hooks_config = load_hooks_config(config, blocked_commands)
    if not hooks_config.is_empty:
        logger.info(
            "Custom hooks: %s",
            ", ".join(
                f"{event} x{len(entries)}"
                for event, entries in hooks_config.hooks.items()
                if entries
            ),
        )
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
        if selected_backend == "cli":
            raise ValueError(
                "limits.max_budget_usd requires backend 'api'; it is not supported "
                "in subscription CLI mode"
            )
        if selected_agent == "codex":
            raise ValueError("limits.max_budget_usd is supported only for agent 'claude'")
    max_changed_files = limits.get("max_changed_files", 8)
    if not isinstance(max_changed_files, int) or max_changed_files <= 0:
        raise ValueError("limits.max_changed_files must be a positive integer")
    phase_timeout_seconds = limits.get("phase_timeout_seconds", 600)
    if not isinstance(phase_timeout_seconds, int) or phase_timeout_seconds <= 0:
        raise ValueError("limits.phase_timeout_seconds must be a positive integer")

    if subagents_config_path is not None:
        # An explicit file is full control: the target overlay does not apply.
        subagents = load_subagents_config(subagents_config_path)
        subagents_source = f"explicit --subagents-config ({subagents_config_path})"
    else:
        subagents, subagents_source = load_subagents_with_target_overlay(
            resource_root / "configs" / "subagents.default.yaml", resolved_repo_path
        )
    logger.info("Subagents: %s", subagents_source)
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
    worktree_created = False
    reused_worktree = False
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
            else:
                worktree_created = True
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

    if checkpoint_enabled_for_run:
        initial_status = get_git_status(active_repo_path).strip()
        if initial_status:
            raise RuntimeError(
                "git.commit_on_success requires a clean agent worktree at task start"
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
        "subagents source": subagents_source,
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
        "worktree created": "yes" if worktree_created else "no",
        "worktree reused": "yes" if reused_worktree else "no",
        "worktree cleanup": "not requested",
        "commit on success": "enabled" if commit_on_success else "disabled",
        "checkpoint commit": "not created",
        "verification": "not run",
        "fix attempts": 0,
        "changed files": 0,
        "next steps": "Review the saved phase outputs before allowing any follow-up action.",
    }

    review_verdict: ReviewVerdict | None = None
    try:
        if setup_only:
            status = "dry-run-setup" if dry_run else "setup-complete"
            details["next steps"] = "Run a planner phase separately; no agent phase was invoked."
            _finalize_git_details(details, active_repo_path)
            _cleanup_worktree_if_requested(
                details=details,
                source_repo_path=resolved_repo_path,
                worktree_path=worktree_path,
                created_for_run=worktree_created,
                requested=delete_worktree_on_success,
            )
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
            resumed_planner_output: str | None = None
            if resume_from_run_dir is not None:
                resumed_planner_output = load_resumable_planner_output(
                    resume_from_run_dir.expanduser().resolve()
                )
                if resumed_planner_output is None:
                    logger.info(
                        "No resumable planner output in %s; running the planner.",
                        resume_from_run_dir,
                    )
            if resumed_planner_output is not None:
                planner_output = resumed_planner_output
                details["planner"] = f"resumed from {resume_from_run_dir}"
                logger.info("Reusing planner output from %s", resume_from_run_dir)
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
                    hooks_config=hooks_config,
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
                _record_run_history(
                    memory_config, task=task, status=status, fix_attempts=0, run_dir=run_dir
                )
                _finalize_git_details(details, active_repo_path)
                _cleanup_worktree_if_requested(
                    details=details,
                    source_repo_path=resolved_repo_path,
                    worktree_path=worktree_path,
                    created_for_run=worktree_created,
                    requested=delete_worktree_on_success,
                )
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
                hooks_config=hooks_config,
            )
            _write_markdown(run_dir / "implementer_output.md", implementer_output)
            diff = get_git_diff(active_repo_path)
            _write_markdown(run_dir / "diff_after_implementer.patch", diff or "# No Diff\n")
            _enforce_changed_file_limit(
                active_repo_path, max_changed_files, "implementer"
            )

            verification_results = run_verification_commands(
                active_repo_path,
                raw_commands,
                blocked_commands,
                timeout_seconds,
                hooks_config=hooks_config,
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
                    hooks_config=hooks_config,
                )
                _write_markdown(run_dir / f"fixer_output_attempt_{fix_attempts}.md", fixer_output)
                _enforce_changed_file_limit(
                    active_repo_path, max_changed_files, f"fixer attempt {fix_attempts}"
                )
                verification_results = run_verification_commands(
                    active_repo_path,
                    raw_commands,
                    blocked_commands,
                    timeout_seconds,
                    hooks_config=hooks_config,
                )
                _write_verification(
                    run_dir / f"verification_attempt_{fix_attempts + 1}.txt", verification_results
                )
                passed = verification_passed(verification_results)

            final_diff = get_git_diff(active_repo_path)
            _enforce_changed_file_limit(active_repo_path, max_changed_files, "fixer loop")
            changed_tests = _enforce_testing_policy(
                active_repo_path,
                policy=testing_policy,
                test_patterns=test_patterns,
                forbid_test_deletion=forbid_test_deletion,
            )
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
                    request_verdict=True,
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
                hooks_config=hooks_config,
            )
            _write_markdown(run_dir / "reviewer_output.md", reviewer_output)
            review_verdict = extract_verdict(reviewer_output)
            if review_verdict is not None:
                (run_dir / VERDICT_FILE_NAME).write_text(
                    review_verdict.to_json() + "\n", encoding="utf-8"
                )
                details["review verdict"] = review_verdict.verdict
                details["review findings"] = len(review_verdict.findings)
            else:
                details["review verdict"] = "not provided"

            status_lines = _changed_files(active_repo_path)
            details["verification"] = "passed" if passed else "failed after fix limit"
            details["fix attempts"] = fix_attempts
            details["changed files"] = len(status_lines)
            details["changed tests"] = len(changed_tests)
            details["diff lines"] = len(final_diff.splitlines())
            if not passed:
                details["risks"] = (
                    "Verification remains failing; inspect fixer and reviewer output."
                )
            elif review_verdict is not None and review_verdict.verdict == "reject":
                details["risks"] = "Reviewer rejected the change; do not land it."
                details["next steps"] = "Inspect and resolve the reviewer findings."
            elif review_verdict is not None and review_verdict.verdict == "revise":
                details["risks"] = "Reviewer requested changes before acceptance."
                details["next steps"] = "Run a bounded revision pass for the reviewer findings."
            else:
                details["risks"] = "Review the reviewer output before committing any changes."
            details["memory"] = _record_memory_update(memory_config, reviewer_output)
            status = _result_status(passed, review_verdict)
            _record_run_history(
                memory_config, task=task, status=status, fix_attempts=fix_attempts, run_dir=run_dir
            )
            if commit_on_success:
                if review_verdict is None or review_verdict.verdict != "approve":
                    details["checkpoint commit"] = "skipped (explicit approval required)"
                elif not get_git_status(active_repo_path).strip():
                    details["checkpoint commit"] = "skipped (no changes)"
                else:
                    assert isinstance(selected_agent_branch, str)
                    revision = commit_all_changes(
                        active_repo_path,
                        _checkpoint_message(task),
                        selected_agent_branch,
                    )
                    details["checkpoint commit"] = revision

        _finalize_git_details(details, active_repo_path)
        _cleanup_worktree_if_requested(
            details=details,
            source_repo_path=resolved_repo_path,
            worktree_path=worktree_path,
            created_for_run=worktree_created,
            requested=delete_worktree_on_success
            if status in {"completed", "plan-only-complete", "setup-complete"}
            else delete_worktree_on_failure,
        )
        report_path = write_report(run_dir, task, status, details)
        return OrchestrationResult(
            run_dir,
            report_path,
            status,
            active_repo_path,
            worktree_path,
            review_verdict=review_verdict,
        )
    except Exception as error:
        details["failure"] = str(error)
        if not dry_run and not setup_only:
            _record_run_history(
                memory_config, task=task, status="failed", fix_attempts=0, run_dir=run_dir
            )
        _finalize_git_details(details, active_repo_path)
        _cleanup_worktree_if_requested(
            details=details,
            source_repo_path=resolved_repo_path,
            worktree_path=worktree_path,
            created_for_run=worktree_created,
            requested=delete_worktree_on_failure,
        )
        write_report(run_dir, task, "failed", details)
        raise
