"""Tests for structured reviewer verdicts."""

from agent.review_gate import VERDICT_INSTRUCTION, ReviewVerdict, extract_verdict


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
