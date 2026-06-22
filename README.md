# Claude Loop Orchestrator

`claude-loop-orchestrator` is a lightweight external control plane for a bounded Claude-assisted repository workflow:

```text
task -> target validation -> optional worktree -> planner -> implementer
     -> verification -> fixer loop -> reviewer -> report
```

It is designed to keep orchestration policy outside the target repository. It does not commit, push, delete branches, remove worktrees, or modify protected branches.

## Architecture

- `agent/main.py` provides the CLI.
- `agent/orchestrator.py` owns phase sequencing, run artifacts, verification, and reporting.
- `agent/claude_runner.py` runs Claude Code safely as `claude -p <prompt>` with an argument list and no shell.
- `agent/sdk_runner.py` is an optional lazy adapter for the Claude Agent SDK.
- `agent/hooks.py` provides deterministic pre/post command and phase guardrails.
- `agent/subagents.py` loads named phase settings from YAML; it does not create recursive SDK subagents.
- `agent/git_utils.py` creates local-only worktrees and rejects protected agent branch names.

## Backends

The default backend is the Claude Code CLI. A normal install does not require the Agent SDK:

```bash
python -m pip install -e ".[dev]"
```

Install the optional SDK adapter only when needed:

```bash
python -m pip install -e ".[dev,sdk]"
```

The SDK is imported only when `--backend sdk` is selected. The adapter intentionally fails clearly if the installed SDK does not expose the supported `query`/`ClaudeAgentOptions` boundary.

## Guardrails and subagents

Blocked command substrings are configured in YAML and are checked before verification execution. Defaults block commits, pushes, W&B sweeps, notebook execution, destructive Docker teardown, and recursive deletion.

`configs/subagents.default.yaml` defines the planner, implementer, fixer, and reviewer. Planner and reviewer are read-only by convention; this version records their allowed-tool configuration but does not yet enforce SDK-native tool permissions for CLI calls.

## Worktree isolation

Set `project.use_worktree` or pass `--use-worktree` to create a new local branch plus linked worktree. The agent branch cannot be `main`, `master`, `develop`, `production`, or `release/*`. Worktree creation fetches only, then uses `git worktree add -b`; it never pushes.

## Dry run

Dry-run writes run metadata under `runs/<timestamp>/` but does not call Claude, run verification commands, create a worktree, or modify the target repository:

```bash
python -m agent.main --repo-path . --task "test task" --dry-run
```

Run artifacts are ignored by Git except `runs/.gitkeep`.

## Plan-only mode

Use `--plan-only` for the first real Claude interaction with an external repository. It creates run metadata, invokes only the read-only planner, captures Git status/diff, writes `planner_output.md`, and stops before implementation, verification, fixing, or review. It is incompatible with `--setup-only`; combined with `--dry-run`, it simulates the same plan-only artifacts without calling Claude.

The planner prompt explicitly requires plan-only behavior: no file edits, modification commands, commits, or pushes. With `--use-worktree`, plan-only reuses the selected existing worktree when one is present, otherwise it safely creates the configured local worktree first.

## Target-local configuration

The orchestrator is project-agnostic. Put project-specific branches, verification commands, persistent domain rules, and project context in the target repository—not in this repository.

When `--config` is omitted, configuration is discovered in this order:

1. `<repo-path>/.agent-loop/config.yaml`
2. `<repo-path>/.agent-loop.yaml`
3. This repository's generic `configs/default.yaml`

Pass `--config path/to/config.yaml` to override discovery explicitly. Every run records both the selected path and selection method in `config_source.md` and `report.md`.

Example target-local configuration:

```yaml
project:
  name: example-project
  use_worktree: true

verification:
  timeout_seconds: 120
  commands:
    - git diff --check

project_context:
  rules:
    - Preserve the target project's documented runtime integrations.
    - Keep changes in the requested scope.
```

For a target repository with local configuration, no `--config` flag is needed:

```powershell
.\.venv\Scripts\python.exe -m agent.main `
  --repo-path ../external/GM-Board `
  --task "Improve action tab chip rendering while preserving behavior." `
  --backend cli `
  --use-worktree `
  --base-branch feature/unified-character-storage `
  --agent-branch agent/improve-action-chip-rendering
```

## Safety notes

- Do not use this tool to bypass target-repository policy.
- Review all proposed changes and reports before committing.
- Do not enable real Claude execution in automated tests.
- The orchestrator never performs `git commit` or `git push`.
- Worktrees are never removed automatically in this version.

## Development

```bash
python -m compileall agent
pytest -q
ruff check .
```

When using the repository virtual environment on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Roadmap

1. Enforce allowed tools through stable SDK lifecycle hooks when the SDK contract is finalized.
2. Add optional plan-only execution as a first-class mode.
3. Add configurable review gates before verification and branch handoff.
4. Add optional cleanup policies that require explicit user confirmation.
