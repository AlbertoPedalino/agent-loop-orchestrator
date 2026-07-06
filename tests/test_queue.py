"""Tests for the file-based task queue."""

from datetime import datetime, timedelta
from pathlib import Path
import json

import pytest
import yaml

from agent.orchestrator import OrchestrationResult
from agent.queue import (
    QueueTask,
    QueueTaskError,
    claim_next,
    enqueue,
    finish_task,
    list_queue,
    parse_queue_task,
    requeue_for_retry,
    run_queue,
)
from agent.review_gate import ReviewVerdict


def _write_task(path: Path, *, priority: int = 0, **overrides: object) -> Path:
    data: dict[str, object] = {
        "repo_path": "C:/repos/example",
        "task": "do the thing",
        "priority": priority,
        **overrides,
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _result(tmp_path: Path, status: str = "completed", **kwargs: object) -> OrchestrationResult:
    run_dir = tmp_path / "runs" / status
    run_dir.mkdir(parents=True, exist_ok=True)
    return OrchestrationResult(
        run_dir=run_dir,
        report_path=run_dir / "report.md",
        status=status,
        target_repo_path=tmp_path,
        **kwargs,
    )


def test_parse_requires_repo_path(tmp_path: Path) -> None:
    task_file = tmp_path / "task.yaml"
    task_file.write_text(yaml.safe_dump({"task": "x"}), encoding="utf-8")

    with pytest.raises(QueueTaskError, match="repo_path"):
        parse_queue_task(task_file)


def test_parse_validates_queue_metadata(tmp_path: Path) -> None:
    task_file = _write_task(tmp_path / "task.yaml", max_retries=-1)
    with pytest.raises(QueueTaskError, match="max_retries"):
        parse_queue_task(task_file)

    task_file = _write_task(tmp_path / "task2.yaml", not_before="not-a-date")
    with pytest.raises(QueueTaskError, match="not_before"):
        parse_queue_task(task_file)


def test_parse_defaults(tmp_path: Path) -> None:
    task = parse_queue_task(_write_task(tmp_path / "task.yaml"))

    assert task.priority == 0
    assert task.max_retries == 1
    assert task.attempts == 0
    assert not task.retry_on_verification_failure
    assert task.not_before is None
    assert task.resume_from is None


def test_enqueue_validates_and_copies(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    source = _write_task(tmp_path / "my-task.yaml")

    destination = enqueue(queue_dir, source)

    assert destination.parent == (queue_dir / "queued").resolve()
    assert "my-task" in destination.name
    assert list_queue(queue_dir)["queued"] == [destination.name]


def test_claim_orders_by_priority_then_name(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "b-low.yaml", priority=0)
    _write_task(queued / "a-high.yaml", priority=5)

    first = claim_next(queue_dir)
    second = claim_next(queue_dir)

    assert first is not None and first.name == "a-high.yaml"
    assert second is not None and second.name == "b-low.yaml"
    assert claim_next(queue_dir) is None


def test_claim_skips_deferred_tasks(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    _write_task(queued / "deferred.yaml", not_before=future)

    assert claim_next(queue_dir) is None
    assert list_queue(queue_dir)["queued"] == ["deferred.yaml"]


def test_claim_moves_invalid_task_to_failed(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    (queued / "broken.yaml").write_text("task: missing repo path", encoding="utf-8")

    assert claim_next(queue_dir) is None
    states = list_queue(queue_dir)
    assert states["queued"] == []
    assert states["failed"] == ["broken.yaml"]
    sidecar = json.loads(
        (queue_dir / "failed" / "broken.result.json").read_text(encoding="utf-8")
    )
    assert sidecar["status"] == "invalid"


def test_finish_task_writes_sidecar(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "task.yaml")
    task = claim_next(queue_dir)
    assert task is not None

    destination = finish_task(queue_dir, task, "done", {"status": "completed"})

    assert destination.parent == (queue_dir / "done").resolve()
    sidecar = json.loads(destination.with_suffix(".result.json").read_text(encoding="utf-8"))
    assert sidecar["status"] == "completed"
    assert sidecar["attempts"] == 1


def test_requeue_records_backoff_and_resume(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "task.yaml")
    task = claim_next(queue_dir)
    assert task is not None

    requeued = requeue_for_retry(
        queue_dir,
        task,
        error="boom",
        resume_from=tmp_path / "runs" / "123",
        backoff_base_seconds=60,
    )

    reparsed = parse_queue_task(requeued)
    assert reparsed.attempts == 1
    assert reparsed.not_before is not None
    assert reparsed.not_before > datetime.now() + timedelta(seconds=30)
    assert reparsed.resume_from == tmp_path / "runs" / "123"
    assert reparsed.raw["last_error"] == "boom"


def test_run_queue_processes_all_tasks(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "one.yaml")
    _write_task(queued / "two.yaml")

    summary = run_queue(queue_dir, executor=lambda task: _result(tmp_path), poll_seconds=0.01)

    assert summary.succeeded == 2
    assert summary.failed == 0
    assert len(list_queue(queue_dir)["done"]) == 2


def test_run_queue_retries_crash_then_succeeds(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "flaky.yaml", max_retries=2)
    calls: list[int] = []

    def flaky_executor(task: QueueTask) -> OrchestrationResult:
        calls.append(task.attempts)
        if len(calls) < 3:
            raise RuntimeError("transient crash")
        return _result(tmp_path)

    summary = run_queue(
        queue_dir, executor=flaky_executor, backoff_base_seconds=0, poll_seconds=0.01
    )

    assert calls == [0, 1, 2]
    assert summary.succeeded == 1
    assert summary.retried == 2
    assert summary.failed == 0


def test_run_queue_exhausted_retries_fail(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "doomed.yaml", max_retries=1)

    def always_crash(task: QueueTask) -> OrchestrationResult:
        raise RuntimeError("permanent crash")

    summary = run_queue(
        queue_dir, executor=always_crash, backoff_base_seconds=0, poll_seconds=0.01
    )

    assert summary.failed == 1
    assert summary.retried == 1
    sidecar = json.loads(
        (queue_dir / "failed" / "doomed.result.json").read_text(encoding="utf-8")
    )
    assert "permanent crash" in sidecar["error"]
    assert sidecar["attempts"] == 2


def test_run_queue_retries_verification_failure_with_resume(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(
        queued / "verify.yaml", max_retries=1, retry_on_verification_failure=True
    )
    seen_resume: list[Path | None] = []

    def executor(task: QueueTask) -> OrchestrationResult:
        seen_resume.append(task.resume_from)
        if len(seen_resume) == 1:
            return _result(tmp_path, status="verification-failed")
        return _result(tmp_path)

    summary = run_queue(
        queue_dir, executor=executor, backoff_base_seconds=0, poll_seconds=0.01
    )

    assert summary.succeeded == 1
    assert summary.retried == 1
    assert seen_resume[0] is None
    # The retry carries the failed attempt's run directory to resume the plan.
    assert seen_resume[1] == tmp_path / "runs" / "verification-failed"


def test_run_queue_verification_failure_without_retry_flag_fails(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "verify.yaml", max_retries=3)

    summary = run_queue(
        queue_dir,
        executor=lambda task: _result(tmp_path, status="verification-failed"),
        backoff_base_seconds=0,
        poll_seconds=0.01,
    )

    assert summary.failed == 1
    assert summary.retried == 0


def test_run_queue_records_review_verdict(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "reviewed.yaml")
    verdict = ReviewVerdict(verdict="revise", findings=[{"severity": "low"}])

    run_queue(
        queue_dir,
        executor=lambda task: _result(tmp_path, review_verdict=verdict),
        poll_seconds=0.01,
    )

    sidecar = json.loads(
        (queue_dir / "done" / "reviewed.result.json").read_text(encoding="utf-8")
    )
    assert sidecar["review_verdict"] == "revise"
    assert sidecar["review_findings"] == 1


def test_run_queue_parallel_requires_worktree(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    _write_task(queued / "no-worktree.yaml")

    summary = run_queue(
        queue_dir, workers=2, executor=lambda task: _result(tmp_path), poll_seconds=0.01
    )

    assert summary.failed == 1
    sidecar = json.loads(
        (queue_dir / "failed" / "no-worktree.result.json").read_text(encoding="utf-8")
    )
    assert "worktree isolation" in sidecar["error"]


def test_run_queue_parallel_accepts_worktree_tasks(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    for index in range(3):
        _write_task(
            queued / f"task-{index}.yaml",
            use_worktree=True,
            base_branch="main",
            agent_branch=f"agent/task-{index}",
        )

    summary = run_queue(
        queue_dir, workers=2, executor=lambda task: _result(tmp_path), poll_seconds=0.01
    )

    assert summary.succeeded == 3
    assert summary.failed == 0


def test_claim_moves_unparseable_yaml_to_failed(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    (queued / "bad.yaml").write_text("task: [unclosed", encoding="utf-8")

    assert claim_next(queue_dir) is None
    assert list_queue(queue_dir)["failed"] == ["bad.yaml"]


def test_parallel_workers_never_claim_a_task_twice(tmp_path: Path) -> None:
    """Regression: a bare rename is not a mutex on Windows (rename-by-handle),
    so claiming must use the exclusive marker; every task runs exactly once."""
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    task_count = 20
    for index in range(task_count):
        _write_task(
            queued / f"task-{index:02d}.yaml",
            use_worktree=True,
            base_branch="main",
            agent_branch=f"agent/task-{index}",
        )
    import threading

    seen: list[str] = []
    seen_lock = threading.Lock()

    def executor(task: QueueTask) -> OrchestrationResult:
        with seen_lock:
            seen.append(task.name)
        return _result(tmp_path)

    summary = run_queue(queue_dir, workers=4, executor=executor, poll_seconds=0.01)

    assert summary.succeeded == task_count
    assert summary.failed == 0
    assert sorted(seen) == sorted(f"task-{index:02d}.yaml" for index in range(task_count))


def test_run_queue_honours_max_tasks(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queued = queue_dir / "queued"
    queued.mkdir(parents=True)
    for index in range(3):
        _write_task(queued / f"task-{index}.yaml")

    summary = run_queue(
        queue_dir, max_tasks=2, executor=lambda task: _result(tmp_path), poll_seconds=0.01
    )

    assert summary.processed == 2
    assert summary.stopped_reason == "task limit reached"
    assert len(list_queue(queue_dir)["queued"]) == 1


def test_run_queue_rejects_bad_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workers"):
        run_queue(tmp_path / "queue", workers=0)
    with pytest.raises(ValueError, match="max_tasks"):
        run_queue(tmp_path / "queue", max_tasks=0)
    with pytest.raises(ValueError, match="max_minutes"):
        run_queue(tmp_path / "queue", max_minutes=0)
