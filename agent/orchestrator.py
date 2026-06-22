"""Safe task -> plan -> implement -> verify -> fix -> review orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent.claude_runner import run_claude_prompt
from agent.git_utils import (
    branch_exists,
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
from agent.report import write_report
from agent.sdk_runner import run_agent_sdk_prompt_sync
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


@dataclass(frozen=True)
class OrchestrationResult:
    """Artifacts and target context produced by an orchestration run."""

    run_dir: Path
    report_path: Path
    status: str
    target_repo_path: Path
    worktree_path: Path | None = None


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
) -> str:
    template = subagent.prompt_template.read_text(encoding="utf-8").strip()
    return (
        f"{template}\n\n"
        "## Orchestrator Context\n\n"
        f"Phase: {subagent.name}\n"
        f"Repository: {repo_path}\n\n"
        f"Task:\n{task.strip()}\n"
        + (f"\n{project_context.strip()}\n" if project_context.strip() else "")
        + "\nSafety constraints: do not commit, push, switch to a protected branch, "
        "run blocked commands, or change files outside the requested scope.\n"
        + (f"\n{context.strip()}\n" if context.strip() else "")
    )


def _run_phase(
    *,
    phase: str,
    prompt: str,
    task: str,
    repo_path: Path,
    backend: str,
    subagent: SubagentConfig,
    max_budget_usd: float | None,
) -> str:
    """Dispatch a configured phase through the selected backend."""
    current_branch = get_current_branch(repo_path)
    if is_protected_branch(current_branch):
        raise RuntimeError(
            f"Refusing to run phase '{phase}' on protected branch '{current_branch}'. "
            "Use an isolated agent worktree."
        )
    pre_phase_hook(phase, repo_path, task)
    selected_backend = subagent.backend or backend
    if selected_backend == "cli":
        output = run_claude_prompt(
            prompt,
            repo_path,
            max_turns=subagent.max_turns,
            max_budget_usd=max_budget_usd,
            phase=phase,
        )
    elif selected_backend == "sdk":
        output = run_agent_sdk_prompt_sync(
            prompt,
            repo_path,
            allowed_tools=subagent.allowed_tools,
            max_turns=subagent.max_turns,
            phase=phase,
        )
    else:
        raise ValueError(f"Unsupported backend: {selected_backend}")
    branch_after_phase = get_current_branch(repo_path)
    if is_protected_branch(branch_after_phase):
        raise RuntimeError(
            f"Phase '{phase}' left the repository on protected branch '{branch_after_phase}'."
        )
    post_phase_hook(phase, output)
    return output


def _dry_run_output(phase: str) -> str:
    return (
        f"# Dry-run {phase.title()}\n\n"
        "Claude was not invoked. This phase is simulated and no target repository files were modified."
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


def run_orchestrator(
    *,
    repo_path: Path,
    task: str,
    config_path: Path,
    max_fix_attempts: int | None = None,
    dry_run: bool = False,
    backend: str | None = None,
    use_worktree: bool | None = None,
    worktree_root: Path | None = None,
    base_branch: str | None = None,
    agent_branch: str | None = None,
    remote: str | None = None,
    subagents_config_path: Path | None = None,
    setup_only: bool = False,
    plan_only: bool = False,
) -> OrchestrationResult:
    """Run the safe orchestration loop, or simulate it in ``dry_run`` mode.

    A dry run writes only run metadata. It never calls Claude, runs verification,
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

    config = deepcopy(load_config(config_path))
    project_context = _resolve_project_context(config)
    formatted_project_context = _format_project_context(project_context)
    project_config = _mapping(config, "project")
    backend_config = _mapping(config, "backend")
    git_config = _mapping(config, "git")
    limits = _mapping(config, "limits")
    verification_config = _mapping(config, "verification")
    if max_fix_attempts is not None:
        if max_fix_attempts < 0:
            raise ValueError("max_fix_attempts must be zero or greater")
        limits["max_fix_attempts"] = max_fix_attempts

    selected_backend = backend or backend_config.get("type", "cli")
    if selected_backend not in {"cli", "sdk"}:
        raise ValueError("Backend must be 'cli' or 'sdk'")
    selected_remote = remote or git_config.get("remote", "origin")
    if not isinstance(selected_remote, str) or not selected_remote.strip():
        raise ValueError("Git remote must be a non-empty string")
    selected_use_worktree = (
        use_worktree if use_worktree is not None else bool(project_config.get("use_worktree", False))
    )
    selected_base_branch = base_branch or git_config.get("base_branch")
    selected_agent_branch = agent_branch or git_config.get("agent_branch")
    if selected_use_worktree:
        if not isinstance(selected_base_branch, str) or not selected_base_branch.strip():
            raise ValueError("A base branch is required when using a worktree")
        if not isinstance(selected_agent_branch, str) or not selected_agent_branch.strip():
            raise ValueError("An agent branch is required when using a worktree")
        if is_protected_branch(selected_agent_branch):
            raise ValueError(f"Refusing protected agent branch: {selected_agent_branch}")
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
    max_changed_files = limits.get("max_changed_files", 8)
    if not isinstance(max_changed_files, int) or max_changed_files <= 0:
        raise ValueError("limits.max_changed_files must be a positive integer")

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
    if formatted_project_context:
        _write_markdown(run_dir / "project_context.md", formatted_project_context)
    _write_markdown(run_dir / "pipeline.md", "# Intended Pipeline\n\n" + "\n".join(
        f"{index}. {step}" for index, step in enumerate(PIPELINE_STEPS, start=1)
    ))

    active_repo_path = resolved_repo_path
    worktree_path: Path | None = None
    current_branch = get_current_branch(resolved_repo_path)
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
                print(f"Dry run: {worktree_note}.")
            else:
                print(f"Dry run: would create local worktree for {worktree_note}.")
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

    print("Prepared orchestration pipeline:")
    for index, step in enumerate(PIPELINE_STEPS, start=1):
        print(f"  {index}. {step}")
    print(f"Target repository: {active_repo_path}")
    print(f"Backend: {selected_backend}")
    if dry_run:
        print("Dry run: Claude, verification, worktree creation, and target-repository changes are disabled.")

    details: dict[str, str | int] = {
        "backend": selected_backend,
        "target repository": str(active_repo_path),
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
            details["next steps"] = "Run a planner phase separately; no Claude phase was invoked."
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
            details["risks"] = "No Claude or verification command was executed."
            details["mode"] = "plan-only" if plan_only else "full-pipeline"
            status = "dry-run-plan-only" if plan_only else "dry-run"
        else:
            planner_prompt = _read_prompt(
                subagents["planner"],
                task,
                active_repo_path,
                project_context=formatted_project_context,
            )
            _write_markdown(run_dir / "planner_prompt.md", planner_prompt)
            planner_output = _run_phase(
                phase="planner",
                prompt=planner_prompt,
                task=task,
                repo_path=active_repo_path,
                backend=selected_backend,
                subagent=subagents["planner"],
                max_budget_usd=float(max_budget) if max_budget is not None else None,
            )
            _write_markdown(run_dir / "planner_output.md", planner_output)

            if plan_only:
                planner_status = get_git_status(active_repo_path)
                planner_diff = get_git_diff(active_repo_path)
                _write_markdown(run_dir / "git_status.txt", planner_status or "Working tree clean.")
                _write_markdown(run_dir / "git_diff.patch", planner_diff or "# No Diff\n")
                details["verification"] = "not run (plan-only)"
                details["git status"] = planner_status or "working tree clean"
                details["diff lines"] = len(planner_diff.splitlines())
                details["mode"] = "plan-only"
                details["risks"] = "Planner output is advisory; review it before any implementation."
                details["next steps"] = "Review planner_output.md before allowing implementation."
                status = "plan-only-complete"
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
                ),
                task=task,
                repo_path=active_repo_path,
                backend=selected_backend,
                subagent=subagents["implementer"],
                max_budget_usd=float(max_budget) if max_budget is not None else None,
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
                    ),
                    task=task,
                    repo_path=active_repo_path,
                    backend=selected_backend,
                    subagent=subagents["fixer"],
                    max_budget_usd=float(max_budget) if max_budget is not None else None,
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
                ),
                task=task,
                repo_path=active_repo_path,
                backend=selected_backend,
                subagent=subagents["reviewer"],
                max_budget_usd=float(max_budget) if max_budget is not None else None,
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
            status = "completed" if passed else "verification-failed"

        report_path = write_report(run_dir, task, status, details)
        return OrchestrationResult(run_dir, report_path, status, active_repo_path, worktree_path)
    except Exception as error:
        details["failure"] = str(error)
        write_report(run_dir, task, "failed", details)
        raise
