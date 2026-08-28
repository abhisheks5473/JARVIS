"""The quota governor.

On the free tier, quota is not an optimisation -- it is the constraint the
whole design has to survive. Google enforces requests-per-minute,
tokens-per-minute *and* requests-per-day simultaneously, and breaching any one
returns a 429.

Most hobby agents discover this by dying at 3pm. This module instead:

  * keeps a durable ledger of every call in SQLite, so a restart does not
    reset the day's count,
  * computes the three rolling windows before each call,
  * blocks briefly when only the per-minute window is tight (that is a wait,
    not a failure),
  * degrades capability as the daily budget drains rather than falling off a
    cliff -- cheaper model, less thinking, no vision,
  * refuses non-interactive (scheduled) work first, so your own turns keep
    working longest.

The limits themselves live in config.py and must be set from your project's
live numbers in AI Studio. This module is only as honest as those numbers.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    day            TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    input_tokens   INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    thought_tokens INTEGER DEFAULT 0,
    total_tokens   INTEGER DEFAULT 0,
    latency_ms     INTEGER DEFAULT 0,
    ok             INTEGER DEFAULT 1,
    status         TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_ts  ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls(day);
"""


class Mode(str, Enum):
    """How much capability we can currently afford."""

    NORMAL = "normal"
    CONSERVE = "conserve"     # cheap model, minimal thinking, vision off
    CRITICAL = "critical"     # user-initiated turns only
    EXHAUSTED = "exhausted"   # daily cap reached


class Decision(str, Enum):
    ALLOW = "allow"
    WAIT = "wait"
    DENY = "deny"


@dataclass
class Verdict:
    decision: Decision
    wait_s: float = 0.0
    reason: str = ""
    mode: Mode = Mode.NORMAL

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass
class Snapshot:
    """Everything the HUD needs to draw the burn-down."""

    rpm_used: int
    rpm_limit: int
    tpm_used: int
    tpm_limit: int
    rpd_used: int
    rpd_limit: int
    mode: Mode
    resets_in_s: float
    tokens_today: int

    @property
    def day_fraction_left(self) -> float:
        if self.rpd_limit <= 0:
            return 1.0
        return max(0.0, 1.0 - self.rpd_used / self.rpd_limit)


class QuotaGovernor:
    """Thread-safe. The scheduler and the REPL both hit this."""

    def __init__(self, db_path=None, limits=None) -> None:
        self._db_path = str(db_path or config.QUOTA_DB)
        self._limits = limits or config.QUOTA
        self._tz = ZoneInfo(config.QUOTA_RESET_TZ)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ windows
    def _today(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    def _seconds_to_reset(self) -> float:
        now = datetime.now(self._tz)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return (tomorrow - now).total_seconds()

    def _minute_window(self) -> tuple[int, int]:
        cutoff = time.time() - 60.0
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM calls WHERE ts > ?",
            (cutoff,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def _day_window(self) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM calls WHERE day = ?",
            (self._today(),),
        ).fetchone()
        return int(row[0]), int(row[1])

    def _mode(self, rpd_used: int) -> Mode:
        limit = self._limits.rpd
        if limit <= 0:
            return Mode.NORMAL
        left = 1.0 - rpd_used / limit
        if left <= 0.0:
            return Mode.EXHAUSTED
        if left < self._limits.critical_below:
            return Mode.CRITICAL
        if left < self._limits.conserve_below:
            return Mode.CONSERVE
        return Mode.NORMAL

    # ------------------------------------------------------------ public
    def snapshot(self) -> Snapshot:
        with self._lock:
            rpm, tpm = self._minute_window()
            rpd, tokens_today = self._day_window()
            return Snapshot(
                rpm_used=rpm,
                rpm_limit=self._limits.rpm,
                tpm_used=tpm,
                tpm_limit=self._limits.tpm,
                rpd_used=rpd,
                rpd_limit=self._limits.rpd,
                mode=self._mode(rpd),
                resets_in_s=self._seconds_to_reset(),
                tokens_today=tokens_today,
            )

    def check(self, interactive: bool = True, est_tokens: int = 2000) -> Verdict:
        """Decide whether a call may proceed right now.

        `interactive` distinguishes a turn you asked for from a scheduled job.
        Background work is sacrificed first so your own turns survive longest.
        """
        with self._lock:
            rpm, tpm = self._minute_window()
            rpd, _ = self._day_window()
            mode = self._mode(rpd)

            if mode is Mode.EXHAUSTED:
                hours = self._seconds_to_reset() / 3600.0
                return Verdict(
                    Decision.DENY,
                    reason=(
                        f"Daily request cap reached ({rpd}/{self._limits.rpd}). "
                        f"Resets in {hours:.1f}h."
                    ),
                    mode=mode,
                )

            if mode is Mode.CRITICAL and not interactive:
                return Verdict(
                    Decision.DENY,
                    reason="Daily budget nearly gone; background work suspended.",
                    mode=mode,
                )

            # Per-minute pressure is a wait, not a failure. Waiting a few
            # seconds beats eating a 429 and burning a retry on it.
            if rpm >= self._limits.rpm:
                oldest = self._conn.execute(
                    "SELECT ts FROM calls WHERE ts > ? ORDER BY ts ASC LIMIT 1",
                    (time.time() - 60.0,),
                ).fetchone()
                wait = max(0.5, 60.0 - (time.time() - oldest[0])) if oldest else 5.0
                return Verdict(
                    Decision.WAIT,
                    wait_s=min(wait, 61.0),
                    reason=f"RPM {rpm}/{self._limits.rpm}",
                    mode=mode,
                )

            # A request larger than the entire per-minute allowance can never
            # fit, however long we wait. Returning WAIT there makes the caller
            # spin through every retry and fail anyway, which presents as a
            # hang. Say plainly that the limit is misconfigured instead.
            if self._limits.tpm and est_tokens >= self._limits.tpm:
                return Verdict(
                    Decision.DENY,
                    reason=(
                        f"a single request needs about {est_tokens} tokens but "
                        f"JARVIS_TPM is {self._limits.tpm}; waiting cannot help. "
                        "Set it to your project's real figure from AI Studio."
                    ),
                    mode=mode,
                )

            if tpm + est_tokens > self._limits.tpm:
                return Verdict(
                    Decision.WAIT,
                    wait_s=8.0,
                    reason=f"TPM {tpm}/{self._limits.tpm}",
                    mode=mode,
                )

            return Verdict(Decision.ALLOW, mode=mode)

    def record(
        self,
        model: str,
        kind: str = "agent",
        input_tokens: int = 0,
        output_tokens: int = 0,
        thought_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: int = 0,
        ok: bool = True,
        status: str = "",
    ) -> None:
        """Log one API call.

        Record failures too: a 429 still counted against the per-minute window
        as far as Google is concerned, and pretending otherwise makes the
        governor optimistic exactly when it must not be.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (ts, day, model, kind, input_tokens, output_tokens,"
                " thought_tokens, total_tokens, latency_ms, ok, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    self._today(),
                    model,
                    kind,
                    input_tokens,
                    output_tokens,
                    thought_tokens,
                    total_tokens or (input_tokens + output_tokens + thought_tokens),
                    latency_ms,
                    1 if ok else 0,
                    status[:200],
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------ reporting
    def daily_report(self, days: int = 7) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, COUNT(*), COALESCE(SUM(total_tokens),0),"
                " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0),"
                " COALESCE(AVG(latency_ms),0)"
                " FROM calls GROUP BY day ORDER BY day DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [
            {
                "day": r[0],
                "requests": r[1],
                "tokens": r[2],
                "failures": r[3],
                "avg_latency_ms": round(r[4]),
            }
            for r in rows
        ]

    def by_kind(self, day: str | None = None) -> list[dict]:
        """Where is the quota actually going? Usually a surprise."""
        day = day or self._today()
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(total_tokens),0) FROM calls"
                " WHERE day = ? GROUP BY kind ORDER BY 3 DESC",
                (day,),
            ).fetchall()
        return [{"kind": r[0], "requests": r[1], "tokens": r[2]} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# A single process-wide governor. Sharing it is the point -- separate
# instances would each believe they had the full budget.
governor = QuotaGovernor()
