# Claude Loop Orchestrator

A lightweight, external Python orchestrator for running a controlled Claude Code development loop against an existing repository.

The intended workflow is:

```text
task -> planner -> implementer -> tests -> fixer loop -> reviewer -> report
```

## Current status

This repository is a skeleton only. It creates a timestamped run directory, records the task, loads YAML configuration, and displays the planned phases. It does not call Claude Code, edit the target repository, create commits, or run verification commands yet.

## Requirements

- Python 3.11+

Install the project and development dependencies:

```bash
python -m pip install -e ".[dev]"
```

## Dry run

From this project directory:

```bash
python -m agent.main --repo-path . --task "test task" --dry-run
```

This writes metadata only under `runs/<timestamp>/`; it does not modify `--repo-path` or invoke Claude.

Run the tests with:

```bash
pytest -q
```

## Configuration

The CLI uses `configs/default.yaml` by default. Pass another file with `--config` to use project-specific limits, verification commands, command blocks, and rules:

```bash
python -m agent.main \
  --repo-path /path/to/project \
  --task "Describe the requested change" \
  --config configs/project.example.yaml \
  --max-fix-attempts 3 \
  --dry-run
```

`configs/project.example.yaml` demonstrates a target-specific configuration. Configuration loading is intentionally simple at this stage: select the file that represents the desired settings.

## Roadmap

1. CLI wrapper around `claude -p`.
2. Worktree isolation.
3. Real planner, implementer, and fixer phases.
4. Claude Agent SDK integration.
5. Hooks and subagents.
