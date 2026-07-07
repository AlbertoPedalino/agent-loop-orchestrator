"""Tests for API-key billing environment handling."""

from pathlib import Path

import pytest

from agent.api_keys import ApiKeyError, load_env_file, resolve_api_key, subprocess_env


def test_load_env_file_parses_minimal_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# ignored",
                "export ANTHROPIC_API_KEY='anthropic-from-file'",
                'OPENAI_API_KEY="openai-from-file"',
                "MALFORMED",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert load_env_file(env_file) == {
        "ANTHROPIC_API_KEY": "anthropic-from-file",
        "OPENAI_API_KEY": "openai-from-file",
    }


def test_resolve_api_key_prefers_process_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=file-key\n", encoding="utf-8")

    assert (
        resolve_api_key(
            "claude",
            environ={"ANTHROPIC_API_KEY": "env-key"},
            env_file=env_file,
        )
        == "env-key"
    )


def test_resolve_codex_api_key_accepts_alias_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CODEX_API_KEY=codex-file-key\n", encoding="utf-8")

    assert resolve_api_key("codex", environ={}, env_file=env_file) == "codex-file-key"


def test_resolve_api_key_reports_missing_key(tmp_path: Path) -> None:
    with pytest.raises(ApiKeyError, match="OPENAI_API_KEY, CODEX_API_KEY"):
        resolve_api_key("codex", environ={}, env_file=tmp_path / ".env")


def test_cli_backend_strips_provider_key_variables() -> None:
    env = subprocess_env(
        "claude",
        "cli",
        environ={
            "PATH": "kept",
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_AUTH_TOKEN": "token",
        },
    )

    assert env == {"PATH": "kept"}


def test_api_backend_injects_claude_key_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=file-key\n", encoding="utf-8")

    env = subprocess_env(
        "claude",
        "api",
        environ={"ANTHROPIC_AUTH_TOKEN": "subscription-token"},
        env_file=env_file,
    )

    assert env["ANTHROPIC_API_KEY"] == "file-key"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_api_backend_injects_codex_key_into_both_supported_names() -> None:
    env = subprocess_env("codex", "api", environ={"OPENAI_API_KEY": "openai-key"})

    assert env["OPENAI_API_KEY"] == "openai-key"
    assert env["CODEX_API_KEY"] == "openai-key"


def test_subprocess_env_rejects_unknown_provider_or_backend() -> None:
    with pytest.raises(ValueError, match="Unknown agent provider"):
        subprocess_env("other", "api", environ={})
    with pytest.raises(ValueError, match="backend"):
        subprocess_env("codex", "sdk", environ={})
