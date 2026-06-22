"""Minimal orchestration setup for future Claude Code phases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent.report import write_report

PIPELINE_STEPS = (
    "planner (inspect and plan)",
    "implementer (make minimal changes)",
    "tests (run configured verification)",
    "fixer loop (repair failing verification)",
    "reviewer (read-only review)",
    "report (record outcome)",
)


@dataclass(frozen=True)
class OrchestrationResult:
    """Artifacts created while preparing an orchestration run."""

    run_dir: Path
    report_path: Path


def load_config(config_path: Path) -> dict[str, Any]:
    """Load a YAML configuration file as a mapping."""
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    if not isinstance(config, dict):
        raise ValueError("Configuration root must be a YAML mapping")
    return config


def _create_run_dir(runs_root: Path) -> Path:
    """Create a unique timestamped directory for a run."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = runs_root / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def run_orchestrator(
    *,
    repo_path: Path,
    task: str,
    config_path: Path,
    max_fix_attempts: int | None = None,
    dry_run: bool = False,
) -> OrchestrationResult:
    """Create run artifacts and display the future orchestration pipeline.

    This skeleton deliberately does not invoke Claude, alter the target repository,
    or execute verification commands. A dry run still records local run metadata.
    """
    resolved_repo_path = repo_path.expanduser().resolve()
    if not resolved_repo_path.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {resolved_repo_path}")

    config = load_config(config_path.expanduser().resolve())
    limits = config.setdefault("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("Configuration limits value must be a YAML mapping")
    if max_fix_attempts is not None:
        limits["max_fix_attempts"] = max_fix_attempts

    project_root = Path(__file__).resolve().parent.parent
    run_dir = _create_run_dir(project_root / "runs")
    (run_dir / "task.md").write_text(f"# Task\n\n{task.strip()}\n", encoding="utf-8")
    (run_dir / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    print("Prepared orchestration pipeline:")
    for index, step in enumerate(PIPELINE_STEPS, start=1):
        print(f"  {index}. {step}")
    print(f"Target repository: {resolved_repo_path}")
    print(f"Max fix attempts: {limits.get('max_fix_attempts', 3)}")
    if dry_run:
        print("Dry run: Claude and target-repository changes are disabled.")
    else:
        print("Skeleton mode: Claude execution and repository changes are not implemented.")

    status = "dry-run" if dry_run else "skeleton"
    report_path = write_report(
        run_dir=run_dir,
        task=task,
        status=status,
        details={
            "repository": str(resolved_repo_path),
            "config": str(config_path.expanduser().resolve()),
            "max_fix_attempts": str(limits.get("max_fix_attempts", 3)),
        },
    )
    return OrchestrationResult(run_dir=run_dir, report_path=report_path)
