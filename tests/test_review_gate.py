"""Tests for structured reviewer verdicts."""

from pathlib import Path

from agent.review_gate import (
    VERDICT_FILE_NAME,
    VERDICT_INSTRUCTION,
    ReviewVerdict,
    extract_verdict,
    format_findings_for_task,
    load_verdict,
)


def test_extract_returns_last_valid_block() -> None:
    output = (
        "analysis prose\n"
        '```verdict\n{"verdict": "reject", "findings": []}\n```\n'
        "more reasoning\n"
        '```verdict\n{"verdict": "approve", "findings": []}\n```\n'
    )

    verdict = extract_verdict(output)

    assert verdict == ReviewVerdict(verdict="approve", findings=[])


def test_extract_parses_findings() -> None:
    output = (
        "```verdict\n"
        '{"verdict": "revise", "findings": ['
        '{"severity": "high", "file": "a.py", "summary": "off-by-one"}]}\n'
        "```"
    )

    verdict = extract_verdict(output)

    assert verdict is not None
    assert verdict.verdict == "revise"
    assert verdict.findings == [{"severity": "high", "file": "a.py", "summary": "off-by-one"}]


def test_extract_none_on_missing_or_malformed() -> None:
    assert extract_verdict("no block") is None
    assert extract_verdict("```verdict\nnot json\n```") is None
    assert extract_verdict('```verdict\n{"verdict": "maybe", "findings": []}\n```') is None
    assert extract_verdict('```verdict\n["approve"]\n```') is None
    assert extract_verdict('```verdict\n{"verdict": "approve", "findings": ["x"]}\n```') is None


def test_to_json_round_trips() -> None:
    verdict = ReviewVerdict(verdict="approve", findings=[])
    assert '"verdict": "approve"' in verdict.to_json()


def test_instruction_mentions_all_verdicts() -> None:
    for value in ("approve", "revise", "reject"):
        assert value in VERDICT_INSTRUCTION


def test_load_verdict_round_trips_saved_file(tmp_path: Path) -> None:
    verdict = ReviewVerdict(
        verdict="revise", findings=[{"severity": "high", "file": "a.py", "summary": "bug"}]
    )
    (tmp_path / VERDICT_FILE_NAME).write_text(verdict.to_json() + "\n", encoding="utf-8")

    assert load_verdict(tmp_path) == verdict


def test_load_verdict_none_when_missing_or_broken(tmp_path: Path) -> None:
    assert load_verdict(tmp_path) is None

    (tmp_path / VERDICT_FILE_NAME).write_text("not json", encoding="utf-8")
    assert load_verdict(tmp_path) is None


def test_format_findings_lists_each_finding() -> None:
    verdict = ReviewVerdict(
        verdict="revise",
        findings=[
            {"severity": "high", "file": "a.py", "summary": "off-by-one"},
            {"severity": "low", "file": "b.py", "summary": "naming"},
        ],
    )

    section = format_findings_for_task(verdict)

    assert "Reviewer Findings to Address" in section
    assert "- [high] a.py: off-by-one" in section
    assert "- [low] b.py: naming" in section


def test_format_findings_handles_empty_list() -> None:
    section = format_findings_for_task(ReviewVerdict(verdict="revise", findings=[]))
    assert "listed no findings" in section
