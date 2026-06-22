"""Markdown reporting for orchestration runs."""

from __future__ import annotations

from pathlib import Path


def write_report(
    run_dir: Path, task: str, status: str, details: dict[str, str]
) -> Path:
    """Write and return a simple Markdown report for a run."""
    detail_lines = "\n".join(f"- **{key}**: {value}" for key, value in details.items())
    report = (
        "# Orchestration Report\n\n"
        f"**Status:** {status}\n\n"
        "## Task\n\n"
        f"{task.strip()}\n\n"
        "## Details\n\n"
        f"{detail_lines}\n"
    )
    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path
