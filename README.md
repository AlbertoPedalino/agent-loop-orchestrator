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

## In-place agent branch

When you want to run the loop *inside* the target repository instead of a separate worktree, use in-place branch mode. The orchestrator creates or checks out the configured agent branch directly in the current target repository.

Branch placement is controlled by two fields (run file or CLI):

- `branch_mode`: `worktree` | `in_place` | `none`
  - `worktree` — separate linked worktree (the existing behavior); `use_worktree: true` implies this.
  - `in_place` — create/checkout the agent branch in the same repository directory.
  - `none` — run on the current branch without creating a branch (default).
- `create_branch`: `auto` | `always` | `never`
  - `auto` (default) — branch only for a full implementation loop; never for `plan_only`, and not for `setup_only` unless requested.
  - `always` — always create/checkout the agent branch (e.g. to prepare a `setup_only` run).
  - `never` — never create a branch.

CLI equivalents: `--branch-mode`, `--create-branch`, `--allow-dirty`.

What happens for a full in-place implementation loop:

1. Refuse unless the working tree is clean (override with `allow_dirty: true` / `--allow-dirty`); a dirty branch is never switched away from.
2. If the agent branch already exists locally, check it out and reuse it.
3. Otherwise fetch the remote only when the base branch is not already available, then create the agent branch from the base: `git checkout -b agent/<name> <base>`.
4. Run planner → implementer → verification → fixer → reviewer on that branch.

Safety: the agent branch must be set, must differ from the base branch, and cannot be a protected name (`main`, `master`, `develop`, `production`, `release/*`). The orchestrator never commits, pushes, merges, or deletes branches. The report records the original/resolved repo paths, branch mode, create-branch mode, original branch, base branch, agent branch, whether a branch was created or reused, the final branch, and whether the working tree is dirty at the end.

### Recommended: analysis task (no branch)

```powershell
cd C:\Users\alber\Documents\DND\GM-Board

C:\Users\alber\Documents\dev\agent-loop-orchestrator\.venv\Scripts\python.exe -m agent.main `
  --run-file .agent-loop\tasks\inspect-character-builder.yaml
```

```yaml
backend: cli
use_worktree: false
branch_mode: in_place
create_branch: auto
plan_only: true

task: |
  Inspect the current Character Builder structure.
  Do not modify files.
```

With `plan_only: true` and `create_branch: auto` no branch is created or checked out; the planner runs in the current repository and a report is produced.

### Recommended: implementation task (in-place agent branch)

```powershell
cd C:\Users\alber\Documents\DND\GM-Board

C:\Users\alber\Documents\dev\agent-loop-orchestrator\.venv\Scripts\python.exe -m agent.main `
  --run-file .agent-loop\tasks\improve-action-chips.yaml
```

```yaml
backend: cli
use_worktree: false
branch_mode: in_place
create_branch: auto
base_branch: feature/unified-character-storage
agent_branch: agent/improve-action-chips
plan_only: false

task: |
  Improve action chip rendering in the Character Builder while preserving existing behavior.
  Keep the change minimal.
```

This creates/checks out `agent/improve-action-chips` from `feature/unified-character-storage` in the same repository directory, runs the full loop on it, and reports the final branch and diff status.

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

## Run files

For repeated agent loops the command line becomes long. A run file is a small
YAML document that captures every parameter for one run, so it can be launched
with a single short command:

```powershell
.\.venv\Scripts\python.exe -m agent.main --run-file tasks/examples/inspect-character-builder.example.yaml
```

A tracked example lives at `tasks/examples/inspect-character-builder.example.yaml`.
It is an example, not a required project config—copy it to your own task folder
(for example `tasks/gm-board/inspect-character-builder.yaml`) and adjust it:

```powershell
.\.venv\Scripts\python.exe -m agent.main --run-file tasks/gm-board/inspect-character-builder.yaml
```

### Recommended: launch from inside the target repository

`repo_path` is optional. The recommended workflow is to keep the run file inside
the target repository next to its `.agent-loop/config.yaml`, then launch from the
target repository so `repo_path` defaults to the current working directory and
target-local config discovery finds `.agent-loop/config.yaml` automatically:

```text
GM-Board/
└── .agent-loop/
    ├── config.yaml
    └── tasks/
        └── inspect-character-builder.yaml
```

```powershell
cd C:\Users\alber\Documents\dev\external\GM-Board

C:\Users\alber\Documents\dev\agent-loop-orchestrator\.venv\Scripts\python.exe -m agent.main `
  --run-file .agent-loop\tasks\inspect-character-builder.yaml
```

With this layout the run file omits `repo_path` entirely and only describes the
task and run parameters.

The target repository is resolved in this order:

1. an explicit `--repo-path` (the one supported CLI override of a run file);
2. the run file's `repo_path`, when present;
3. otherwise the current working directory.

Each run records the run-file path, the resolved repo path, and which of these
three sources it came from (`run_source.md` and `report.md`).

### Supported fields

```yaml
repo_path: string | null # optional; defaults to --repo-path or the working dir
config: string | null    # optional; omit to keep target-local config discovery
backend: cli | sdk       # defaults to cli
use_worktree: boolean    # defaults to false; true implies branch_mode: worktree
branch_mode: worktree | in_place | none  # optional; see "In-place agent branch"
create_branch: auto | always | never     # optional; defaults to auto
allow_dirty: boolean     # defaults to false; permit switching with a dirty tree
base_branch: string | null
agent_branch: string | null
plan_only: boolean       # defaults to false
setup_only: boolean      # defaults to false
dry_run: boolean         # defaults to false
task: string             # either task or task_file is required (mutually exclusive)
task_file: string | null
```

Rules: `repo_path` is optional (see the resolution order above); exactly one of
`task` or `task_file` is required; booleans default to `false`; `backend` defaults
to `cli`. When `config` is omitted, the usual target-local discovery applies
(`.agent-loop/config.yaml`, then `.agent-loop.yaml`, then the generic fallback).

### Path handling

The `--run-file` path and any relative paths inside a run file (`repo_path`,
`config`, `task_file`) are resolved **relative to the current working directory
from which the command is launched** unless they are absolute. This matches the
behavior of passing the same paths directly on the command line.

### Exclusivity

`--run-file` is mutually exclusive with every other run-parameter flag
(`--task`, `--task-file`, `--config`, `--backend`, `--use-worktree`,
`--branch-mode`, `--create-branch`, `--allow-dirty`, `--base-branch`,
`--agent-branch`, `--remote`, `--worktree-root`,
`--max-fix-attempts`, `--plan-only`, `--setup-only`, `--dry-run`). The only
supported override is `--repo-path`, which takes precedence over the run file's
`repo_path`. Combining any other flag fails clearly so the source of each
parameter stays unambiguous. To dry-run or plan-only a run file, set `dry_run` or
`plan_only` inside the file. Each run records its source (`run-file` or
`cli-args`), the resolved repo path, the repo-path source, and the run-file path
in `run_source.md` and `report.md`.

### Example: plan-only analysis

```yaml
repo_path: ../external/GM-Board
backend: cli
use_worktree: true
base_branch: feature/unified-character-storage
agent_branch: agent/inspect-character-builder-structure
plan_only: true

task: |
  Inspect the current Character Builder structure in GM-Board.
  Do not modify files. Produce a clear architectural report.
```

### Example: full implementation loop

```yaml
repo_path: ../external/GM-Board
backend: cli
use_worktree: true
base_branch: feature/unified-character-storage
agent_branch: agent/improve-action-chip-rendering

task: |
  Improve action tab chip rendering while preserving behavior.
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
