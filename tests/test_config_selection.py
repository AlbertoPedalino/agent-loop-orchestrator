"""Tests for generic target-local configuration discovery."""

from pathlib import Path
import sys

from agent import main as main_module
from agent.orchestrator import ConfigSelection, OrchestrationResult, resolve_config_selection


def test_explicit_config_path_takes_precedence(tmp_path: Path) -> None:
    explicit_path = tmp_path / "explicit.yaml"
    explicit_path.write_text("project: {}\n", encoding="utf-8")
    (tmp_path / ".agent-loop").mkdir()
    (tmp_path / ".agent-loop" / "config.yaml").write_text("project: {}\n", encoding="utf-8")

    selection = resolve_config_selection(tmp_path, explicit_path)

    assert selection == ConfigSelection(explicit_path.resolve(), "explicit --config")


def test_discovers_target_local_directory_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".agent-loop" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("project: {}\n", encoding="utf-8")

    selection = resolve_config_selection(tmp_path)

    assert selection == ConfigSelection(
        config_path.resolve(), "target-local .agent-loop/config.yaml"
    )


def test_discovers_target_local_single_file_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".agent-loop.yaml"
    config_path.write_text("project: {}\n", encoding="utf-8")

    selection = resolve_config_selection(tmp_path)

    assert selection == ConfigSelection(config_path.resolve(), "target-local .agent-loop.yaml")


def test_falls_back_to_generic_default(tmp_path: Path) -> None:
    fallback_path = tmp_path / "default.yaml"
    fallback_path.write_text("project: {}\n", encoding="utf-8")

    selection = resolve_config_selection(tmp_path, fallback_config_path=fallback_path)

    assert selection == ConfigSelection(
        fallback_path.resolve(), "packaged fallback agent/resources/configs/default.yaml"
    )


def test_packaged_fallback_and_phase_resources_exist(tmp_path: Path) -> None:
    selection = resolve_config_selection(tmp_path)
    resource_root = selection.path.parent.parent

    assert selection.path.is_file()
    assert (resource_root / "configs" / "subagents.default.yaml").is_file()
    assert {path.name for path in (resource_root / "prompts").glob("*.md")} == {
        "planner.md",
        "implementer.md",
        "fixer.md",
        "reviewer.md",
    }


def test_cli_uses_target_local_config_when_config_is_omitted(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / ".agent-loop" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("project: {}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_orchestrator(**kwargs: object) -> OrchestrationResult:
        captured.update(kwargs)
        return OrchestrationResult(tmp_path, tmp_path / "report.md", "dry-run", tmp_path)

    monkeypatch.setattr(main_module, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.main", "--repo-path", str(tmp_path), "--task", "task", "--dry-run"],
    )

    assert main_module.main() == 0
    assert captured["config_path"] == config_path.resolve()
    assert captured["config_source"] == "target-local .agent-loop/config.yaml"


def test_cli_uses_explicit_config_path(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "explicit.yaml"
    config_path.write_text("project: {}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_orchestrator(**kwargs: object) -> OrchestrationResult:
        captured.update(kwargs)
        return OrchestrationResult(tmp_path, tmp_path / "report.md", "dry-run", tmp_path)

    monkeypatch.setattr(main_module, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent.main",
            "--repo-path",
            str(tmp_path),
            "--task",
            "task",
            "--config",
            str(config_path),
            "--dry-run",
        ],
    )

    assert main_module.main() == 0
    assert captured["config_path"] == config_path.resolve()
    assert captured["config_source"] == "explicit --config"
