"""File-based task queue with parallel workers, retries, and resume.

The queue is a directory with four state subdirectories::

    <queue root>/
      queued/    tasks waiting to run (YAML, run-file fields + queue metadata)
      running/   tasks claimed by a worker
      done/      finished tasks plus a ``<name>.result.json`` sidecar
      failed/    exhausted tasks plus the same sidecar

A task file carries the exact same fields as a ``--run-file`` plus queue-only
metadata (``id``, ``depends_on``, ``priority``, retry/review flags, and retry/review limits);
``attempts``, ``review_cycles``, ``not_before``, and ``resume_from`` are managed by the queue
itself across retries/revisions. ``repo_path`` is required: a queued task must be
self-contained because it does not inherit a working directory.

Claiming uses an exclusive ``.claim`` marker (``O_CREAT | O_EXCL``) followed by
a move from ``queued/`` to ``running/``, so multiple workers (threads or
separate processes) can safely share one queue. A bare rename is *not* a
reliable mutex on Windows: ``MoveFileExW`` opens the source by name and renames
by handle, so two racing renames of the same file can both report success.
Exclusive file creation is atomic everywhere. Retries are re-enqueued with an
exponential-backoff ``not_before`` timestamp and, when the failed attempt
already produced a plan, a ``resume_from`` pointer so the retry skips the
planner phase.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
import hashlib
import json
import os
import threading
import time

import yaml

from agent.log import get_logger
from agent.orchestrator import (
    OrchestrationResult,
    load_resumable_planner_output,
    resolve_config_selection,
    run_orchestrator,
)
from agent.review_gate import format_findings_for_task, load_verdict
from agent.run_file import RunFileConfig, parse_run_data, resolve_task_text

logger = get_logger()

# Anchored to the orchestrator repository, not the caller's cwd, so `add` can
# run from any target repository without an explicit --queue-dir.
DEFAULT_QUEUE_DIR = Path(__file__).resolve().parent.parent / "tasks" / "queue"
RETRY_BACKOFF_BASE_SECONDS = 30.0

# A claim marker older than this is treated as left behind by a crashed worker
# (the claim-to-move window is milliseconds) and may be removed.
_CLAIM_STALE_SECONDS = 300.0

_STATE_DIRS = ("queued", "running", "done", "failed")


class _RepositoryLock:
    """Cross-process advisory lock serializing in-place work per repository."""

    def __init__(self, queue_dir: Path, repo_path: Path) -> None:
        identity = str(repo_path.expanduser().resolve()).casefold().encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        lock_dir = queue_dir.expanduser().resolve() / ".repo-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self.path = lock_dir / f"{digest}.lock"
        self._handle: Any = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def acquire(self, poll_seconds: float) -> None:
        while not self.try_acquire():
            time.sleep(poll_seconds)

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

# Queue-only metadata; everything else in a task file is a run-file field.
# "last_error" is written by requeue helpers and must survive re-parsing.
_QUEUE_ONLY_FIELDS = frozenset(
    {
        "priority",
        "id",
        "depends_on",
        "max_retries",
        "retry_on_verification_failure",
        "retry_on_review_revise",
        "max_review_cycles",
        "review_cycles",
        "findings_from",
        "attempts",
        "not_before",
        "resume_from",
        "last_error",
    }
)


class QueueTaskError(ValueError):
    """Raised when a queue task file is invalid."""


@dataclass(frozen=True)
class QueueTask:
    """A parsed queue task: run parameters plus queue metadata."""

    path: Path
    run: RunFileConfig
    raw: dict[str, Any]
    task_id: str | None = None
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    max_retries: int = 1
    retry_on_verification_failure: bool = False
    retry_on_review_revise: bool = False
    max_review_cycles: int = 1
    review_cycles: int = 0
    attempts: int = 0
    not_before: datetime | None = None
    resume_from: Path | None = None
    findings_from: Path | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def uses_worktree(self) -> bool:
        return self.run.use_worktree or self.run.branch_mode == "worktree"


@dataclass
class QueueSummary:
    """Aggregate outcome of one ``run_queue`` invocation."""

    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    revised: int = 0
    stopped_reason: str = "queue empty"

    @property
    def processed(self) -> int:
        return self.succeeded + self.failed


def queue_state_dirs(queue_dir: Path) -> dict[str, Path]:
    """Create (if needed) and return the queue's state directories."""
    resolved = queue_dir.expanduser().resolve()
    dirs = {state: resolved / state for state in _STATE_DIRS}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _require_int(data: dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QueueTaskError(f"Queue task '{key}' must be an integer >= {minimum}")
    return value


def _optional_non_empty_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise QueueTaskError(f"Queue task '{key}' must be a non-empty string")
    return value.strip()


def _optional_string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise QueueTaskError(f"Queue task '{key}' must be a list of non-empty strings")
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise QueueTaskError(f"Queue task '{key}' must be a list of non-empty strings")
        normalized = item.strip()
        if normalized in seen:
            raise QueueTaskError(f"Queue task '{key}' contains duplicate id '{normalized}'")
        seen.add(normalized)
        items.append(normalized)
    return tuple(items)


def _load_task_data(path: Path) -> dict[str, Any]:
    """Read a task file into a mapping, normalizing load errors."""
    if not path.is_file():
        raise FileNotFoundError(f"Queue task file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as error:
        raise QueueTaskError(f"Queue task file is not valid YAML: {error}") from error
    if data is None:
        raise QueueTaskError(f"Queue task file is empty: {path}")
    if not isinstance(data, dict):
        raise QueueTaskError("Queue task root must be a YAML mapping")
    return data


def parse_queue_task(path: Path) -> QueueTask:
    """Load and validate one queue task file."""
    resolved = path.expanduser().resolve()
    data = _load_task_data(resolved)
    return _parse_queue_data(data, resolved)


def _parse_queue_data(data: dict[str, Any], resolved: Path) -> QueueTask:
    """Validate an already-loaded queue task mapping."""
    queue_meta = {key: data[key] for key in _QUEUE_ONLY_FIELDS if key in data}
    run_data = {key: value for key, value in data.items() if key not in _QUEUE_ONLY_FIELDS}
    run = parse_run_data(run_data)
    if run.repo_path is None:
        raise QueueTaskError("Queue tasks must set 'repo_path' explicitly")

    task_id = _optional_non_empty_string(queue_meta, "id")
    depends_on = _optional_string_list(queue_meta, "depends_on")
    if depends_on and task_id is None:
        raise QueueTaskError("Queue task 'depends_on' requires queue task 'id'")
    if task_id is not None and task_id in depends_on:
        raise QueueTaskError("Queue task 'depends_on' cannot include its own id")

    priority = _require_int(queue_meta, "priority", 0, -1_000_000)
    max_retries = _require_int(queue_meta, "max_retries", 1, 0)
    attempts = _require_int(queue_meta, "attempts", 0, 0)
    max_review_cycles = _require_int(queue_meta, "max_review_cycles", 1, 0)
    review_cycles = _require_int(queue_meta, "review_cycles", 0, 0)
    retry_on_verification_failure = queue_meta.get("retry_on_verification_failure", False)
    if not isinstance(retry_on_verification_failure, bool):
        raise QueueTaskError("Queue task 'retry_on_verification_failure' must be a boolean")
    retry_on_review_revise = queue_meta.get("retry_on_review_revise", False)
    if not isinstance(retry_on_review_revise, bool):
        raise QueueTaskError("Queue task 'retry_on_review_revise' must be a boolean")

    not_before: datetime | None = None
    not_before_value = queue_meta.get("not_before")
    if not_before_value is not None:
        if isinstance(not_before_value, datetime):
            not_before = not_before_value
        elif isinstance(not_before_value, str):
            try:
                not_before = datetime.fromisoformat(not_before_value)
            except ValueError as error:
                raise QueueTaskError(
                    f"Queue task 'not_before' must be an ISO timestamp: {not_before_value}"
                ) from error
        else:
            raise QueueTaskError("Queue task 'not_before' must be an ISO timestamp string")

    resume_from_value = queue_meta.get("resume_from")
    if resume_from_value is not None and not isinstance(resume_from_value, str):
        raise QueueTaskError("Queue task 'resume_from' must be a string path")
    findings_from_value = queue_meta.get("findings_from")
    if findings_from_value is not None and not isinstance(findings_from_value, str):
        raise QueueTaskError("Queue task 'findings_from' must be a string path")

    return QueueTask(
        path=resolved,
        run=run,
        raw=data,
        task_id=task_id,
        depends_on=depends_on,
        priority=priority,
        max_retries=max_retries,
        retry_on_verification_failure=retry_on_verification_failure,
        retry_on_review_revise=retry_on_review_revise,
        max_review_cycles=max_review_cycles,
        review_cycles=review_cycles,
        attempts=attempts,
        not_before=not_before,
        resume_from=Path(resume_from_value) if resume_from_value else None,
        findings_from=Path(findings_from_value) if findings_from_value else None,
    )


def _unique_destination(directory: Path, name: str) -> Path:
    """Return a non-existing destination path, suffixing on collision."""
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _rename_with_retry(source: Path, destination: Path) -> None:
    """Rename a queue file, tolerating brief Windows reader locks."""
    for attempt in range(5):
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def enqueue(
    queue_dir: Path,
    source: Path,
    *,
    default_repo_path: Path | None = None,
    queue_metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Validate *source* and copy it into ``queued/`` under a unique name.

    A task file kept inside its target repository can omit ``repo_path`` and
    stay location-independent; *default_repo_path* (the CLI passes the launch
    directory) is stamped into the queued copy at enqueue time, so the copy is
    self-contained and workers can run from anywhere. Optional queue metadata
    is also stamped only into the queued copy. The source file is never modified.
    """
    resolved_source = source.expanduser().resolve()
    data = _load_task_data(resolved_source)
    stamped = False
    if data.get("repo_path") is None and default_repo_path is not None:
        data = {**data, "repo_path": str(default_repo_path.expanduser().resolve())}
        stamped = True
    if queue_metadata:
        unknown = set(queue_metadata) - _QUEUE_ONLY_FIELDS
        if unknown:
            raise QueueTaskError(
                f"Unknown queue metadata fields: {', '.join(sorted(unknown))}"
            )
        data = {**data, **dict(queue_metadata)}
        stamped = True
    task = _parse_queue_data(data, resolved_source)
    dirs = queue_state_dirs(queue_dir)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = _unique_destination(dirs["queued"], f"{timestamp}-{task.path.stem}.yaml")
    if stamped:
        destination.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        logger.info(
            "Enqueued %s -> %s (repo_path stamped: %s)", source, destination, data["repo_path"]
        )
    else:
        destination.write_text(resolved_source.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Enqueued %s -> %s", source, destination)
    return destination


def list_queue(queue_dir: Path) -> dict[str, list[str]]:
    """Return task file names per queue state."""
    dirs = queue_state_dirs(queue_dir)
    return {
        state: sorted(path.name for path in dirs[state].glob("*.yaml"))
        for state in _STATE_DIRS
    }


@dataclass(frozen=True)
class _DependencySnapshot:
    """Resolved task ids currently visible in queue state directories."""

    done_ids: frozenset[str] = frozenset()
    failed_ids: frozenset[str] = frozenset()
    running_ids: frozenset[str] = frozenset()
    queued_ids: frozenset[str] = frozenset()
    duplicate_ids: frozenset[str] = frozenset()

    @property
    def known_ids(self) -> frozenset[str]:
        return self.done_ids | self.failed_ids | self.running_ids | self.queued_ids


def _dependency_snapshot(dirs: dict[str, Path]) -> _DependencySnapshot:
    ids_by_state: dict[str, set[str]] = {state: set() for state in _STATE_DIRS}
    locations: dict[str, list[tuple[str, Path]]] = {}
    for state in _STATE_DIRS:
        for path in dirs[state].glob("*.yaml"):
            try:
                task = parse_queue_task(path)
            except (OSError, QueueTaskError, ValueError):
                continue
            if task.task_id is None:
                continue
            ids_by_state[state].add(task.task_id)
            locations.setdefault(task.task_id, []).append((state, path))
    duplicate_ids = {task_id for task_id, paths in locations.items() if len(paths) > 1}
    return _DependencySnapshot(
        done_ids=frozenset(ids_by_state["done"]),
        failed_ids=frozenset(ids_by_state["failed"]),
        running_ids=frozenset(ids_by_state["running"]),
        queued_ids=frozenset(ids_by_state["queued"]),
        duplicate_ids=frozenset(duplicate_ids),
    )


def _find_dependency_cycle_ids(
    queued_tasks: list[QueueTask], snapshot: _DependencySnapshot
) -> frozenset[str]:
    graph: dict[str, set[str]] = {}
    for task in queued_tasks:
        if task.task_id is None or task.task_id in snapshot.duplicate_ids:
            continue
        graph[task.task_id] = {
            dependency
            for dependency in task.depends_on
            if dependency in snapshot.queued_ids and dependency not in snapshot.duplicate_ids
        }

    states: dict[str, str] = {}
    stack: list[str] = []
    cycle_ids: set[str] = set()

    def visit(task_id: str) -> None:
        states[task_id] = "visiting"
        stack.append(task_id)
        for dependency in graph.get(task_id, set()):
            dependency_state = states.get(dependency)
            if dependency_state == "visiting":
                cycle_ids.update(stack[stack.index(dependency):])
            elif dependency_state is None:
                visit(dependency)
        stack.pop()
        states[task_id] = "visited"

    for task_id in graph:
        if states.get(task_id) is None:
            visit(task_id)
    return frozenset(cycle_ids)


def _dependency_failure_reason(
    task: QueueTask, snapshot: _DependencySnapshot, cycle_ids: frozenset[str]
) -> str | None:
    if task.task_id is not None and task.task_id in snapshot.duplicate_ids:
        return f"duplicate queue id '{task.task_id}'"
    if task.task_id is not None and task.task_id in cycle_ids:
        return f"dependency cycle involving id '{task.task_id}'"
    for dependency in task.depends_on:
        if dependency in snapshot.duplicate_ids:
            return f"dependency id '{dependency}' is duplicated"
        if dependency in snapshot.failed_ids:
            return f"dependency '{dependency}' failed"
    return None


def _dependency_wait_summary(task: QueueTask, snapshot: _DependencySnapshot) -> str | None:
    if not task.depends_on:
        return None
    missing: list[str] = []
    queued: list[str] = []
    running: list[str] = []
    for dependency in task.depends_on:
        if dependency in snapshot.done_ids:
            continue
        if dependency in snapshot.running_ids:
            running.append(dependency)
        elif dependency in snapshot.queued_ids:
            queued.append(dependency)
        elif dependency not in snapshot.failed_ids and dependency not in snapshot.duplicate_ids:
            missing.append(dependency)
    parts: list[str] = []
    if queued:
        parts.append("queued " + ", ".join(queued))
    if running:
        parts.append("running " + ", ".join(running))
    if missing:
        parts.append("missing " + ", ".join(missing))
    return "; ".join(parts) if parts else None


def list_queue_details(queue_dir: Path) -> dict[str, list[str]]:
    """Return queue state entries with dependency wait reasons for queued tasks."""
    dirs = queue_state_dirs(queue_dir)
    snapshot = _dependency_snapshot(dirs)
    queued_tasks: list[QueueTask] = []
    for path in dirs["queued"].glob("*.yaml"):
        try:
            queued_tasks.append(parse_queue_task(path))
        except (OSError, QueueTaskError, ValueError):
            continue
    cycle_ids = _find_dependency_cycle_ids(queued_tasks, snapshot)

    details: dict[str, list[str]] = {}
    for state in _STATE_DIRS:
        entries: list[str] = []
        for path in sorted(dirs[state].glob("*.yaml"), key=lambda item: item.name):
            entry = path.name
            if state == "queued":
                try:
                    task = parse_queue_task(path)
                except (QueueTaskError, ValueError) as error:
                    entry += f" (invalid: {error})"
                except OSError:
                    continue
                else:
                    failure = _dependency_failure_reason(task, snapshot, cycle_ids)
                    waiting = _dependency_wait_summary(task, snapshot)
                    if failure is not None:
                        entry += f" (blocked: {failure})"
                    elif waiting is not None:
                        entry += f" (waiting: {waiting})"
            entries.append(entry)
        details[state] = entries
    return details


def _queue_should_keep_waiting(dirs: dict[str, Path]) -> bool:
    """Return whether queued tasks may become ready without a new enqueue."""
    snapshot = _dependency_snapshot(dirs)
    now = datetime.now()
    for path in dirs["queued"].glob("*.yaml"):
        try:
            task = parse_queue_task(path)
        except (OSError, QueueTaskError, ValueError):
            return True
        if task.not_before is not None and task.not_before > now:
            return True
        if any(dependency in snapshot.running_ids for dependency in task.depends_on):
            return True
    return False


def _claim_marker(path: Path) -> Path:
    return path.parent / (path.name + ".claim")


def _try_claim(path: Path) -> bool:
    """Take the exclusive claim marker for *path*, or return ``False``.

    ``O_CREAT | O_EXCL`` is the one primitive that is atomic across processes
    on every platform; renames are not (see the module docstring). A stale
    marker from a crashed worker is removed so the task becomes claimable again
    on the next pass.
    """
    marker = _claim_marker(path)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - marker.stat().st_mtime
            if age > _CLAIM_STALE_SECONDS:
                marker.unlink()
                logger.warning("Removed stale claim marker %s", marker.name)
        except OSError:
            pass
        return False
    except OSError:
        return False
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
    finally:
        os.close(descriptor)
    return True


def _release_claim(path: Path) -> None:
    try:
        _claim_marker(path).unlink()
    except OSError:
        pass


def claim_next(queue_dir: Path, now: datetime | None = None) -> QueueTask | None:
    """Atomically claim the highest-priority ready task, or return ``None``.

    Candidates are ordered by descending priority, then by file name (which
    starts with the enqueue timestamp, so FIFO within a priority). Tasks whose
    ``not_before`` lies in the future are skipped. A candidate another worker
    claims first is skipped without error. An unparseable candidate is moved to
    ``failed/`` so it cannot wedge the queue.
    """
    dirs = queue_state_dirs(queue_dir)
    current_time = now or datetime.now()

    candidates: list[tuple[int, str, Path, QueueTask | None, str]] = []
    for path in dirs["queued"].glob("*.yaml"):
        try:
            task = parse_queue_task(path)
        except (QueueTaskError, ValueError) as error:
            candidates.append((0, path.name, path, None, str(error)))
            continue
        except OSError:
            # Claimed/moved by another worker mid-listing, or a transient
            # Windows sharing violation while its rename is in flight.
            continue
        if task.not_before is not None and task.not_before > current_time:
            continue
        candidates.append((-task.priority, path.name, path, task, ""))

    queued_tasks = [task for _, _, _, task, _ in candidates if task is not None]
    needs_dependency_snapshot = any(
        task.task_id is not None or task.depends_on for task in queued_tasks
    )
    snapshot = _dependency_snapshot(dirs) if needs_dependency_snapshot else _DependencySnapshot()
    cycle_ids = _find_dependency_cycle_ids(queued_tasks, snapshot)

    for _, _, path, task, parse_error in sorted(candidates, key=lambda item: (item[0], item[1])):
        if task is not None:
            dependency_failure = _dependency_failure_reason(task, snapshot, cycle_ids)
            if dependency_failure is not None:
                if path.exists() and _try_claim(path):
                    try:
                        _fail_dependency_task(dirs, path, task, dependency_failure)
                    finally:
                        _release_claim(path)
                continue
            if _dependency_wait_summary(task, snapshot) is not None:
                continue
        if not path.exists() or not _try_claim(path):
            continue
        try:
            if task is None:
                _fail_invalid_task(dirs, path, parse_error)
                continue
            destination = dirs["running"] / path.name
            try:
                _rename_with_retry(path, destination)
            except OSError:
                continue  # vanished under us despite the claim; treat as lost
            return replace(task, path=destination)
        finally:
            _release_claim(path)
    return None


def _fail_invalid_task(dirs: dict[str, Path], path: Path, error: str) -> None:
    """Move an unparseable, already-claimed task to ``failed/``."""
    destination = _unique_destination(dirs["failed"], path.name)
    try:
        _rename_with_retry(path, destination)
    except OSError:
        return
    _write_result_sidecar(
        destination, {"status": "invalid", "error": error, "attempts": 0}
    )
    logger.warning("Moved invalid queue task %s to failed/: %s", path.name, error)


def _task_result_metadata(task: QueueTask) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if task.task_id is not None:
        metadata["id"] = task.task_id
    if task.depends_on:
        metadata["depends_on"] = list(task.depends_on)
    return metadata


def _fail_dependency_task(
    dirs: dict[str, Path], path: Path, task: QueueTask, error: str
) -> None:
    """Move a dependency-impossible queued task to ``failed/``."""
    destination = _unique_destination(dirs["failed"], path.name)
    try:
        _rename_with_retry(path, destination)
    except OSError:
        return
    _write_result_sidecar(
        destination,
        {
            **_task_result_metadata(task),
            "status": "dependency-failed",
            "error": error,
            "dependency_error": error,
            "attempts": task.attempts,
        },
    )
    logger.warning("Moved dependency-blocked queue task %s to failed/: %s", path.name, error)


def _write_result_sidecar(task_path: Path, result: dict[str, Any]) -> Path:
    sidecar = task_path.with_suffix(".result.json")
    payload = {**result, "finished_at": datetime.now().isoformat(timespec="seconds")}
    sidecar.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return sidecar


def finish_task(
    queue_dir: Path, task: QueueTask, outcome: str, result: dict[str, Any]
) -> Path:
    """Move a running task to ``done/`` or ``failed/`` and write its sidecar."""
    if outcome not in {"done", "failed"}:
        raise ValueError("outcome must be 'done' or 'failed'")
    dirs = queue_state_dirs(queue_dir)
    destination = _unique_destination(dirs[outcome], task.path.name)
    _rename_with_retry(task.path, destination)
    _write_result_sidecar(
        destination,
        {**_task_result_metadata(task), **result, "attempts": task.attempts + 1},
    )
    return destination


def requeue_for_retry(
    queue_dir: Path,
    task: QueueTask,
    *,
    error: str,
    resume_from: Path | None = None,
    backoff_base_seconds: float = RETRY_BACKOFF_BASE_SECONDS,
) -> Path:
    """Put a failed running task back into ``queued/`` with backoff metadata.

    The rewritten file records the new attempt count, an exponential-backoff
    ``not_before``, and (when available) the failed run directory to resume the
    plan from, so the retry does not pay for a second planner phase.
    """
    dirs = queue_state_dirs(queue_dir)
    attempts = task.attempts + 1
    delay = backoff_base_seconds * (2 ** (attempts - 1))
    updated = dict(task.raw)
    updated["attempts"] = attempts
    updated["not_before"] = (datetime.now() + timedelta(seconds=delay)).isoformat(
        timespec="seconds"
    )
    updated["last_error"] = " ".join(error.split())[:500]
    if resume_from is not None:
        updated["resume_from"] = str(resume_from)
    destination = dirs["queued"] / task.path.name
    destination.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
    task.path.unlink()
    logger.info(
        "Requeued %s for retry %d/%d (not before %s)",
        task.path.name,
        attempts,
        task.max_retries,
        updated["not_before"],
    )
    return destination


def requeue_for_review_revise(queue_dir: Path, task: QueueTask, run_dir: Path) -> Path:
    """Re-enqueue a task whose reviewer verdict was ``revise``.

    Unlike a crash retry there is no backoff: the run itself succeeded, the
    reviewer just wants another pass. The rewritten task resumes from the
    completed run's plan and carries a pointer to its verdict findings, which
    the executor appends to the task text for the revision pass. Review cycles
    are counted separately from crash retries.
    """
    dirs = queue_state_dirs(queue_dir)
    updated = dict(task.raw)
    updated["review_cycles"] = task.review_cycles + 1
    updated["resume_from"] = str(run_dir)
    updated["findings_from"] = str(run_dir)
    updated["last_error"] = f"reviewer requested revisions (run {run_dir})"
    destination = dirs["queued"] / task.path.name
    destination.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
    task.path.unlink()
    logger.info(
        "Requeued %s for review revision %d/%d",
        task.path.name,
        task.review_cycles + 1,
        task.max_review_cycles,
    )
    return destination


def _task_text_with_findings(task: QueueTask) -> str:
    """Return the task text, extended with reviewer findings on a revision pass."""
    text = resolve_task_text(task.run)
    if task.findings_from is None:
        return text
    verdict = load_verdict(task.findings_from)
    if verdict is None:
        logger.warning("No loadable verdict in %s; running without findings.", task.findings_from)
        return text
    return f"{text.rstrip()}\n\n{format_findings_for_task(verdict)}"


def execute_task(task: QueueTask, *, stream: bool = True) -> OrchestrationResult:
    """Run one queue task through the orchestrator (the default executor)."""
    assert task.run.repo_path is not None  # guaranteed by parse_queue_task
    selection = resolve_config_selection(task.run.repo_path, task.run.config)
    resume_from = task.resume_from
    if resume_from is not None and load_resumable_planner_output(resume_from) is None:
        resume_from = None
    return run_orchestrator(
        repo_path=task.run.repo_path,
        task=_task_text_with_findings(task),
        config_path=selection.path,
        config_source=selection.source,
        agent=task.run.agent,
        backend=task.run.backend,
        use_worktree=task.run.use_worktree,
        branch_mode=task.run.branch_mode,
        create_branch=task.run.create_branch,
        allow_dirty=task.run.allow_dirty,
        base_branch=task.run.base_branch,
        agent_branch=task.run.agent_branch,
        setup_only=task.run.setup_only,
        plan_only=task.run.plan_only,
        dry_run=task.run.dry_run,
        run_file_path=task.path,
        launched_from="queue",
        repo_path_source="queue task repo_path",
        stream=stream,
        resume_from_run_dir=resume_from,
    )


@dataclass
class _QueueState:
    """Shared coordination state for the worker threads of one run_queue call."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    in_flight: int = 0
    claimed_total: int = 0
    summary: QueueSummary = field(default_factory=QueueSummary)
    stop: bool = False


def _handle_outcome(
    queue_dir: Path,
    task: QueueTask,
    result: OrchestrationResult | None,
    error: Exception | None,
    state: _QueueState,
    backoff_base_seconds: float,
) -> None:
    """Route one finished attempt to done/, failed/, or a retry requeue."""
    if error is not None:
        if task.attempts < task.max_retries:
            requeue_for_retry(
                queue_dir, task, error=str(error), backoff_base_seconds=backoff_base_seconds
            )
            with state.lock:
                state.summary.retried += 1
            return
        finish_task(queue_dir, task, "failed", {"status": "failed", "error": str(error)})
        with state.lock:
            state.summary.failed += 1
        return

    assert result is not None
    payload: dict[str, Any] = {
        "status": result.status,
        "run_dir": str(result.run_dir),
        "report": str(result.report_path),
    }
    if result.review_verdict is not None:
        payload["review_verdict"] = result.review_verdict.verdict
        payload["review_findings"] = len(result.review_verdict.findings)

    if (
        result.status == "verification-failed"
        and task.retry_on_verification_failure
        and task.attempts < task.max_retries
    ):
        requeue_for_retry(
            queue_dir,
            task,
            error=f"verification failed (run {result.run_dir})",
            resume_from=result.run_dir,
            backoff_base_seconds=backoff_base_seconds,
        )
        with state.lock:
            state.summary.retried += 1
        return

    verdict = result.review_verdict.verdict if result.review_verdict is not None else None
    if (
        result.status in {"completed", "review-revise"}
        and verdict == "revise"
        and task.retry_on_review_revise
        and task.review_cycles < task.max_review_cycles
    ):
        requeue_for_review_revise(queue_dir, task, result.run_dir)
        with state.lock:
            state.summary.revised += 1
        return

    # A rejected review gates a verification-green run out of done/: landing it
    # would contradict the reviewer, so a human has to look at failed/.
    rejected = result.status == "review-rejected" or (
        result.status == "completed" and verdict == "reject"
    )
    outcome = (
        "failed"
        if rejected or result.status in {"verification-failed", "review-revise", "failed"}
        else "done"
    )
    if rejected:
        payload["error"] = "reviewer rejected the change"
    finish_task(queue_dir, task, outcome, payload)
    with state.lock:
        if outcome == "done":
            state.summary.succeeded += 1
        else:
            state.summary.failed += 1


def _worker_loop(
    queue_dir: Path,
    state: _QueueState,
    *,
    workers: int,
    deadline: float | None,
    max_tasks: int | None,
    executor: Callable[[QueueTask], OrchestrationResult],
    backoff_base_seconds: float,
    poll_seconds: float,
) -> None:
    dirs = queue_state_dirs(queue_dir)
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            with state.lock:
                state.stop = True
                state.summary.stopped_reason = "time limit reached"
        with state.lock:
            if state.stop:
                return
            if max_tasks is not None and state.claimed_total >= max_tasks:
                state.summary.stopped_reason = "task limit reached"
                return
            # Reserve the claim slot under the lock so concurrent workers
            # cannot overshoot max_tasks between the check and the claim.
            state.claimed_total += 1

        task = claim_next(queue_dir)
        if task is None:
            with state.lock:
                state.claimed_total -= 1
                busy = state.in_flight > 0
            has_pending = any(dirs["queued"].glob("*.yaml"))
            if not busy and not has_pending:
                return  # nothing left anywhere: queue drained
            if not busy and not _queue_should_keep_waiting(dirs):
                with state.lock:
                    state.summary.stopped_reason = "blocked dependencies"
                return
            # Deferred retries or another worker's task may still produce work.
            time.sleep(poll_seconds)
            continue

        if workers > 1 and not task.uses_worktree:
            finish_task(
                queue_dir,
                task,
                "failed",
                {
                    "status": "invalid",
                    "error": (
                        "Parallel queue workers require worktree isolation: set "
                        "use_worktree: true or branch_mode: worktree in the task file."
                    ),
                },
            )
            with state.lock:
                state.summary.failed += 1
            continue

        with state.lock:
            state.in_flight += 1
        logger.info("Queue worker starting %s (attempt %d)", task.name, task.attempts + 1)
        result: OrchestrationResult | None = None
        caught: Exception | None = None
        repository_lock: _RepositoryLock | None = None
        try:
            if not task.uses_worktree:
                assert task.run.repo_path is not None
                repository_lock = _RepositoryLock(queue_dir, task.run.repo_path)
                repository_lock.acquire(poll_seconds)
            result = executor(task)
        except Exception as error:  # noqa: BLE001 - a task crash must not kill the worker
            caught = error
            logger.warning("Queue task %s attempt failed: %s", task.name, error)
        finally:
            if repository_lock is not None:
                repository_lock.release()
        try:
            _handle_outcome(queue_dir, task, result, caught, state, backoff_base_seconds)
        finally:
            with state.lock:
                state.in_flight -= 1


def run_queue(
    queue_dir: Path,
    *,
    workers: int = 1,
    max_tasks: int | None = None,
    max_minutes: float | None = None,
    stream: bool = True,
    executor: Callable[[QueueTask], OrchestrationResult] | None = None,
    backoff_base_seconds: float = RETRY_BACKOFF_BASE_SECONDS,
    poll_seconds: float = 5.0,
) -> QueueSummary:
    """Process queued tasks until the queue drains or a limit is reached.

    ``workers`` threads share the queue; with more than one, every task must
    run in an isolated worktree so parallel runs cannot edit the same working
    tree. ``max_tasks`` bounds how many attempts this invocation claims and
    ``max_minutes`` bounds its wall-clock time — the aggregate limits for an
    unattended queue session. ``executor`` exists for tests.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1 when provided")
    if max_minutes is not None and max_minutes <= 0:
        raise ValueError("max_minutes must be positive when provided")

    state = _QueueState()
    deadline = time.monotonic() + max_minutes * 60 if max_minutes is not None else None
    selected_executor = executor or (lambda task: execute_task(task, stream=stream))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="queue-worker") as pool:
        futures = [
            pool.submit(
                _worker_loop,
                queue_dir,
                state,
                workers=workers,
                deadline=deadline,
                max_tasks=max_tasks,
                executor=selected_executor,
                backoff_base_seconds=backoff_base_seconds,
                poll_seconds=poll_seconds,
            )
            for _ in range(workers)
        ]
        for future in futures:
            future.result()

    logger.info(
        "Queue run finished: %d succeeded, %d failed, %d retried, %d revised (%s)",
        state.summary.succeeded,
        state.summary.failed,
        state.summary.retried,
        state.summary.revised,
        state.summary.stopped_reason,
    )
    return state.summary
