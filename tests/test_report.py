"""Tests for Markdown report generation."""

from pathlib import Path

from agent.report import write_report


def test_write_report(tmp_path: Path) -> None:
    report_path = write_report(tmp_path, "task", "completed", {"fix attempts": 0})

    assert report_path == tmp_path / "report.md"
    assert "**Status:** completed" in report_path.read_text(encoding="utf-8")
