"""Tests for resuming a run from a previous attempt's planner output."""

from pathlib import Path

import pytest

from agent.orchestrator import load_resumable_planner_output, run_orchestrator


def test_returns_planner_output_when_present(tmp_path: Path) -> None:
    (tmp_path / "planner_output.md").write_text("# Plan\n\n1. do X\n", encoding="utf-8")

    assert load_resumable_planner_output(tmp_path) == "# Plan\n\n1. do X"


def test_returns_none_when_missing_empty_or_dry_run(tmp_path: Path) -> None:
    assert load_resumable_planner_output(tmp_path) is None

    planner_path = tmp_path / "planner_output.md"
    planner_path.write_text("   \n", encoding="utf-8")
    assert load_resumable_planner_output(tmp_path) is None

    planner_path.write_text("# Dry-run Planner\n\nsimulated\n", encoding="utf-8")
    assert load_resumable_planner_output(tmp_path) is None


def test_resume_requires_full_loop(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="resume_from_run_dir"):
        run_orchestrator(
            repo_path=tmp_path,
            task="x",
            config_path=tmp_path / "config.yaml",
            plan_only=True,
            resume_from_run_dir=tmp_path,
        )
