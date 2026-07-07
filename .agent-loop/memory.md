# Project Memory

## What this is
`agent-loop-orchestrator` — external control plane running bounded Claude/Codex loops against a target repo: task → validation → optional worktree/branch → planner → implementer → verification → fixer loop → reviewer → report. Never commits/pushes/deletes branches.

## Entry points
- `pyproject.toml [project.scripts]`: `agent-loop = agent.main:main`, `agent-queue = agent.queue_cli:main`.
- Also runnable as `python -m agent.main` / `python -m agent.queue_cli`.
- Repo origin: https://github.com/AlbertoPedalino/agent-loop-orchestrator.git

## Key modules (agent/)
- `main.py` CLI; `orchestrator.py` phase sequencing, run artifacts, reporting.
- `claude_runner.py` (`claude -p`, no shell), `codex_runner.py` (`codex exec`, prompt on stdin, `--output-last-message`).
- `policies.py` blocked-command checks (`validate_commands_allowed`); `verifier.py` runs verification commands under those checks, no shell, collects all results.
- `permissions.py` per-phase tool policy; `WRITE_TOOLS = {Edit, Write, MultiEdit, NotebookEdit, Bash}`; read-only = no write tool allowed.
- `hooks.py` pre/post command+phase hooks; `subagents.py` YAML phase settings; `skills.py` skill policy (Claude: grant `Skill` tool natively; Codex: inline SKILL.md).
- `run_file.py` YAML run-file spec — relative paths resolve against launch cwd, not run-file location.
- `git_utils.py` worktrees + protected-branch guard (`main`, `master`, `develop`, `production`, `release/*`); `remove_worktree` intentionally non-forced (git refuses dirty).
- `queue.py`/`queue_cli.py` file queue under `tasks/queue/` (`queued/running/done/failed`), `.claim` marker mutex (Windows rename not atomic-exclusive).
- `review_gate.py` parses fenced ```verdict``` JSON; `memory.py` project memory + history jsonl; `report.py` writes `report.md`.

## Worktree cleanup (implemented, opt-in)
`git.delete_worktree_on_success` / `git.delete_worktree_on_failure` (both default false, `configs/default.yaml`). `_cleanup_worktree_if_requested` (orchestrator.py, ~line 584): skips when not requested, no worktree, worktree reused (`created_for_run` false), worktree == source repo, or dirty tree. README Roadmap removed 2026-07-07 because this shipped.

## Config discovery
`--config` else `<repo>/.agent-loop/config.yaml` → `<repo>/.agent-loop.yaml` → orchestrator `configs/default.yaml`. Run files: only CLI override allowed is `--repo-path`; all other flags mutually exclusive with `--run-file`.

## Gotchas
- Verification commands run without shell and must be findable executables: bare `pytest -q` fails with `[WinError 2]` when pytest not on orchestrator PATH (seen 2026-07-07 fix-readme run — "failed after fix limit" was environmental, not the diff). Prefer `python -m pytest -q` or absolute venv path in `verification.commands`.
- This dev machine: Windows 11, venv at `.\.venv\Scripts\python.exe`; sandboxed reviewer shells may be denied running venv python or `git remote get-url` — read `.git/config` instead.
- Run artifacts under `runs/<timestamp>/` (gitignored except `.gitkeep`): `report.md`, `phase_events.jsonl`, `verification_attempt_N.txt`, `review_verdict.json`, diffs, prompts.
- README audited 2026-07-07 against source (branch `agent/fix-readme`); statements verified then. Task file used: `.agent-loop/tasks/fix-readme.yaml` (in_place branch mode, create_branch: always, base main).
