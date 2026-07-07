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


def test_add_stamps_cwd_when_repo_path_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Target-local task files omit repo_path; `add` stamps the launch cwd."""
    from agent.queue import parse_queue_task

    queue_dir = tmp_path / "queue"
    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()
    source = target_repo / "task.yaml"
    source.write_text(yaml.safe_dump({"task": "portable"}), encoding="utf-8")
    monkeypatch.chdir(target_repo)
    monkeypatch.setattr(
        "sys.argv", ["queue_cli", "--queue-dir", str(queue_dir), "add", str(source)]
    )

    assert main() == 0
    queued = list((queue_dir / "queued").glob("*.yaml"))
    assert len(queued) == 1
    assert parse_queue_task(queued[0]).run.repo_path == target_repo.resolve()


def test_add_stamps_review_retry_metadata_without_modifying_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent.queue import parse_queue_task

    queue_dir = tmp_path / "queue"
    source = _write_task(tmp_path / "my-task.yaml")
    monkeypatch.setattr(
        "sys.argv",
        [
            "queue_cli",
            "--queue-dir",
            str(queue_dir),
            "add",
            str(source),
            "--retry-on-verification-failure",
            "--max-retries",
            "3",
            "--retry-on-review-revise",
            "--max-review-cycles",
            "2",
        ],
    )

    assert main() == 0
    assert "Enqueued:" in capsys.readouterr().out
    queued = list((queue_dir / "queued").glob("*.yaml"))
    assert len(queued) == 1
    task = parse_queue_task(queued[0])
    assert task.retry_on_verification_failure is True
    assert task.max_retries == 3
    assert task.retry_on_review_revise is True
    assert task.max_review_cycles == 2
    source_data = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert "retry_on_review_revise" not in source_data
    assert "retry_on_verification_failure" not in source_data


def test_add_stamps_dependency_metadata_without_modifying_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent.queue import parse_queue_task

    queue_dir = tmp_path / "queue"
    source = _write_task(tmp_path / "child.yaml")
    monkeypatch.setattr(
        "sys.argv",
        [
            "queue_cli",
            "--queue-dir",
            str(queue_dir),
            "add",
            str(source),
            "--id",
            "child",
            "--depends-on",
            "parent-a",
            "--depends-on",
            "parent-b",
        ],
    )

    assert main() == 0
    assert "Enqueued:" in capsys.readouterr().out
    queued = list((queue_dir / "queued").glob("*.yaml"))
    assert len(queued) == 1
    task = parse_queue_task(queued[0])
    assert task.task_id == "child"
    assert task.depends_on == ("parent-a", "parent-b")
    source_data = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert "id" not in source_data
    assert "depends_on" not in source_data


def test_add_rejects_invalid_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"task": ["not", "a", "string"]}), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["queue_cli", "--queue-dir", str(tmp_path / "queue"), "add", str(bad)]
    )

    with pytest.raises(SystemExit):
        main()


def test_list_reports_dependency_wait_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    (queued / "child.yaml").write_text(
        yaml.safe_dump(
            {
                "repo_path": "C:/repos/example",
                "task": "do child",
                "id": "child",
                "depends_on": ["parent"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["queue_cli", "--queue-dir", str(queue_dir), "list"])

    assert main() == 0
    output = capsys.readouterr().out
    assert "child.yaml (waiting: missing parent)" in output


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
