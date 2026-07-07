"""Structured reviewer verdicts for the orchestration loop.

The reviewer phase is asked to end its reply with a fenced ``verdict`` block
containing a small JSON document. The orchestrator parses it deterministically
so downstream automation (the task queue, a human gate) can branch on
``approve`` / ``revise`` / ``reject`` instead of re-reading free-form prose.
A missing or malformed block degrades gracefully to "no verdict"; the reviewer
prose is still saved in full either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import re

VALID_VERDICTS = frozenset({"approve", "revise", "reject"})

VERDICT_FILE_NAME = "review_verdict.json"

# A fenced ```verdict ... ``` block. The last one in an output wins, mirroring
# the memory-block convention, so the reviewer can reason first and conclude
# with one authoritative verdict.
_VERDICT_BLOCK = re.compile(r"```verdict[ \t]*\n(.*?)\n```", re.DOTALL)

VERDICT_INSTRUCTION = (
    "## Review Verdict (required)\n\n"
    "End your reply with a single fenced block tagged `verdict` containing a "
    "JSON object:\n\n"
    "```verdict\n"
    '{"verdict": "approve", "findings": []}\n'
    "```\n\n"
    "- `verdict` must be `approve` (safe to accept), `revise` (issues that a "
    "fixer pass should address), or `reject` (fundamentally wrong approach).\n"
    "- `findings` is a list of objects with `severity` (`high`|`medium`|`low`), "
    "`file`, and `summary` describing each concrete issue; empty when approving.\n"
)


@dataclass(frozen=True)
class ReviewVerdict:
    """A parsed reviewer verdict."""

    verdict: str
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {"verdict": self.verdict, "findings": self.findings}, sort_keys=True, indent=2
        )


def load_verdict(run_dir: Path) -> ReviewVerdict | None:
    """Load the saved verdict from a run directory, or ``None``.

    Reads the ``review_verdict.json`` the orchestrator writes, applying the
    same graceful degradation as :func:`extract_verdict`.
    """
    verdict_path = run_dir / VERDICT_FILE_NAME
    if not verdict_path.is_file():
        return None
    return extract_verdict(f"```verdict\n{verdict_path.read_text(encoding='utf-8')}\n```")


def format_findings_for_task(verdict: ReviewVerdict) -> str:
    """Format reviewer findings as a task addendum for a revision pass.

    Appended to the task text when a ``revise`` verdict re-enqueues the run, so
    the implementer works from the concrete findings instead of re-deriving
    them from prose.
    """
    lines = [
        "## Reviewer Findings to Address",
        "",
        "A previous run of this task passed verification, but the reviewer "
        "requested revisions. Address these findings without regressing the "
        "existing changes:",
        "",
    ]
    if verdict.findings:
        for finding in verdict.findings:
            severity = str(finding.get("severity", "unspecified"))
            file_name = str(finding.get("file", "unspecified file"))
            summary = str(finding.get("summary", "no summary provided"))
            lines.append(f"- [{severity}] {file_name}: {summary}")
    else:
        lines.append("- The reviewer requested revisions but listed no findings; ")
        lines.append("  re-read the previous reviewer output for context.")
    return "\n".join(lines)


def extract_verdict(output: str) -> ReviewVerdict | None:
    """Return the last valid fenced ``verdict`` block, or ``None``.

    Any malformed block (bad JSON, unknown verdict value, wrong shapes) yields
    ``None`` rather than an exception: the verdict is an optional structured
    layer over the reviewer prose, never a reason to fail the run.
    """
    matches = _VERDICT_BLOCK.findall(output)
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    verdict = parsed.get("verdict")
    if verdict not in VALID_VERDICTS:
        return None
    findings = parsed.get("findings", [])
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        return None
    return ReviewVerdict(verdict=verdict, findings=findings)
