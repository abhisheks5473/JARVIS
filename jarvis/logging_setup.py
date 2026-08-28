"""Logging, because at 3am the log is the only thing that will tell you why.

Agents fail silently and interestingly. A rate limit hits, a retry loop
swallows it, and two days later you notice it has not spoken since Tuesday.
Two separate sinks, for two separate questions:

  * `jarvis.log` -- ordinary application logging. What broke.
  * `transcript.jsonl` -- one JSON object per turn: prompt, tools called,
    tools denied, tokens, latency, model, taint level. What it *did*, in a
    form you can actually query with a one-line script.

Everything written passes through `redact()` first. A log file is exactly the
kind of place an API key ends up and is never noticed.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from . import config
from .security.approval import redact


def _setup() -> logging.Logger:
    logger = logging.getLogger("jarvis")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    handler = RotatingFileHandler(
        config.LOG_DIR / "jarvis.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    # Deliberately no StreamHandler: stdout belongs to the HUD, and a stray
    # log line in the middle of a live panel corrupts the display.
    return logger


log = _setup()


class Transcript:
    """Append-only record of every turn, one JSON object per line."""

    def __init__(self, path=None) -> None:
        self.path = path or (config.LOG_DIR / "transcript.jsonl")

    def _write(self, record: dict[str, Any]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            log.warning("could not write transcript: %s", exc)

    def log_turn(self, user_text: str, report: Any) -> None:
        self._write(
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "ts": time.time(),
                "kind": "turn",
                "input": redact(user_text)[:2000],
                "reply": redact(getattr(report, "reply", ""))[:2000],
                "tools": list(getattr(report, "tool_calls", [])),
                "denied": list(getattr(report, "denied", [])),
                "steps": getattr(report, "steps_used", 0),
                "api_calls": getattr(report, "api_calls", 0),
                "tokens": getattr(report, "tokens", 0),
                "latency_ms": getattr(report, "latency_ms", 0),
                "model": getattr(report, "model", ""),
                "degraded": getattr(report, "degraded", False),
                "fast_path": getattr(report, "fast_path", False),
                "taint": getattr(report, "taint_level", "CLEAN"),
                "error": getattr(report, "error", ""),
            }
        )

    def log_event(self, kind: str, **data: Any) -> None:
        self._write(
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "ts": time.time(),
                "kind": kind,
                **{k: redact(str(v))[:1000] for k, v in data.items()},
            }
        )

    def recent(self, limit: int = 20) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.readlines()[-limit:]
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


transcript = Transcript()
