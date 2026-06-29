"""Central, terminal-friendly logging for orchestration runs.

Logs go to ``stderr`` so the orchestrator's machine-readable result lines (run
directory, report path) stay clean on ``stdout``. Use :func:`configure_logging`
once at process start, then :func:`get_logger` everywhere else.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_LOGGER_NAME = "agent"

# Salient tool-input keys, in display priority order, used to summarize a tool
# call on one line without dumping the full input payload.
_TOOL_INPUT_KEYS = (
    "file_path",
    "path",
    "pattern",
    "command",
    "url",
    "query",
    "prompt",
    "old_string",
)


def configure_logging(*, verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """Configure and return the shared ``agent`` logger.

    ``quiet`` shows warnings and errors only; ``verbose`` adds DEBUG
    (per-message events, raw stream lines that fail to parse); the default is
    INFO.
    """
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    """Return the shared ``agent`` logger (configured lazily at INFO if needed)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        return configure_logging()
    return logger


def _truncate(value: str, limit: int = 80) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 3] + "..."


def summarize_tool_use(name: str, tool_input: dict[str, Any]) -> str:
    """Summarize a tool call as ``Name(key=value, ...)`` for a single log line."""
    parts = [
        f"{key}={_truncate(str(tool_input[key]))}"
        for key in _TOOL_INPUT_KEYS
        if key in tool_input
    ]
    return f"{name}({', '.join(parts)})" if parts else name
