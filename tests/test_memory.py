"""Tests for the accumulated project-memory layer."""

from pathlib import Path

import pytest

from agent.memory import (
    DEFAULT_HISTORY_FILE,
    DEFAULT_MEMORY_FILE,
    append_history,
    extract_memory_block,
    format_history_section,
    format_memory_section,
    load_memory,
    load_recent_history,
    resolve_memory_config,
    update_memory_from_output,
)


def test_resolve_defaults_enabled_under_repo(tmp_path: Path) -> None:
    cfg = resolve_memory_config({}, tmp_path)

    assert cfg.enabled
    assert cfg.path == (tmp_path / DEFAULT_MEMORY_FILE).resolve()


def test_resolve_honours_disabled_and_custom_path(tmp_path: Path) -> None:
    cfg = resolve_memory_config(
        {"memory": {"enabled": False, "file": "docs/knowledge.md"}}, tmp_path
    )

    assert not cfg.enabled
    assert cfg.path == (tmp_path / "docs/knowledge.md").resolve()


def test_resolve_rejects_bad_types(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory.enabled"):
        resolve_memory_config({"memory": {"enabled": "yes"}}, tmp_path)
    with pytest.raises(ValueError, match="memory.file"):
        resolve_memory_config({"memory": {"file": ""}}, tmp_path)


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    cfg = resolve_memory_config({}, tmp_path)
    assert load_memory(cfg) == ""


def test_format_section_empty_when_blank() -> None:
    assert format_memory_section("   ") == ""
    assert "Accumulated Project Memory" in format_memory_section("# Project\nfacts")


def test_extract_returns_last_block() -> None:
    output = (
        "intro\n```memory\nold\n```\nmore prose\n```memory\n# Project\nnew facts\n```\ntail"
    )
    assert extract_memory_block(output) == "# Project\nnew facts"


def test_extract_none_when_absent_or_empty() -> None:
    assert extract_memory_block("no block here") is None
    assert extract_memory_block("```memory\n\n```") is None


def test_update_writes_block_and_skips_when_absent(tmp_path: Path) -> None:
    cfg = resolve_memory_config({}, tmp_path)

    assert update_memory_from_output(cfg, "report\n```memory\n# Project\nA\n```")
    assert cfg.path.read_text(encoding="utf-8") == "# Project\nA\n"

    assert not update_memory_from_output(cfg, "report with no block")
    # Unchanged after a no-block run.
    assert cfg.path.read_text(encoding="utf-8") == "# Project\nA\n"


def test_update_noop_when_disabled(tmp_path: Path) -> None:
    cfg = resolve_memory_config({"memory": {"enabled": False}}, tmp_path)
    assert not update_memory_from_output(cfg, "```memory\nx\n```")
    assert not cfg.path.exists()


def test_resolve_history_defaults(tmp_path: Path) -> None:
    cfg = resolve_memory_config({}, tmp_path)

    assert cfg.history_enabled
    assert cfg.history_path == (tmp_path / DEFAULT_HISTORY_FILE).resolve()


def test_resolve_history_rejects_bad_types(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory.history "):
        resolve_memory_config({"memory": {"history": "yes"}}, tmp_path)
    with pytest.raises(ValueError, match="memory.history_file"):
        resolve_memory_config({"memory": {"history_file": ""}}, tmp_path)


def test_history_append_load_round_trip(tmp_path: Path) -> None:
    cfg = resolve_memory_config({}, tmp_path)

    assert append_history(cfg, {"timestamp": "t1", "status": "completed", "task": "a"})
    assert append_history(cfg, {"timestamp": "t2", "status": "failed", "task": "b"})

    entries = load_recent_history(cfg, limit=1)
    assert [entry["timestamp"] for entry in entries] == ["t2"]
    assert len(load_recent_history(cfg)) == 2


def test_history_disabled_is_noop(tmp_path: Path) -> None:
    cfg = resolve_memory_config({"memory": {"history": False}}, tmp_path)

    assert not append_history(cfg, {"status": "completed"})
    assert load_recent_history(cfg) == []


def test_history_load_skips_malformed_lines(tmp_path: Path) -> None:
    cfg = resolve_memory_config({}, tmp_path)
    cfg.history_path.parent.mkdir(parents=True)
    cfg.history_path.write_text('not json\n{"status": "completed"}\n[1]\n', encoding="utf-8")

    assert load_recent_history(cfg) == [{"status": "completed"}]


def test_format_history_section(tmp_path: Path) -> None:
    assert format_history_section([]) == ""

    section = format_history_section(
        [{"timestamp": "t1", "status": "failed", "fix_attempts": 2, "task": "fix the tests"}]
    )
    assert "Recent Run History" in section
    assert "status=failed" in section
    assert "fix the tests" in section
