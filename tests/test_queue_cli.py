"""Tests for the queue command-line interface."""

from pathlib import Path

import pytest
import yaml

from agent.orchestrator import OrchestrationResult
from agent.queue_cli import build_parser, main


def _write_task(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump({"repo_path": "C:/repos/example", "task": "do the thing"}),
        encoding="utf-8",
    )
    return path


def test_parser_defaults() -> None:
    args = build_parser().parse_args(["run"])

    assert args.command == "run"
    assert args.workers == 1
    assert args.max_tasks is None
    assert args.max_minutes is None
    assert args.stream


def test_add_and_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_dir = tmp_path / "queue"
    source = _write_task(tmp_path / "my-task.yaml")

    monkeypatch.setattr(
        "sys.argv", ["queue_cli", "--queue-dir", str(queue_dir), "add", str(source)]
    )
    assert main() == 0
    assert "Enqueued:" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["queue_cli", "--queue-dir", str(queue_dir), "list"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "queued (1):" in output
    assert "my-task" in output


def test_add_rejects_invalid_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"task": "missing repo path"}), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["queue_cli", "--queue-dir", str(tmp_path / "queue"), "add", str(bad)]
    )

    with pytest.raises(SystemExit):
        main()


def test_run_reports_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "task.yaml")
    run_dir = tmp_path / "runs" / "1"
    run_dir.mkdir(parents=True)

    def fake_execute(task: object, *, stream: bool = True) -> OrchestrationResult:
        return OrchestrationResult(
            run_dir=run_dir,
            report_path=run_dir / "report.md",
            status="completed",
            target_repo_path=tmp_path,
        )

    monkeypatch.setattr("agent.queue.execute_task", fake_execute)
    monkeypatch.setattr(
        "sys.argv", ["queue_cli", "--queue-dir", str(queue_dir), "run", "--max-minutes", "1"]
    )

    assert main() == 0
    output = capsys.readouterr().out
    assert "Succeeded: 1" in output
    assert "Failed: 0" in output
