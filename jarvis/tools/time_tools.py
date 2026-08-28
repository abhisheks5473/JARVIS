"""Time, and the arithmetic people actually ask for.

`get_time` is marked always_available because the model needs it constantly
and the system prompt tells it to call this before any date reasoning. A
model that guesses the date is wrong in ways that are hard to notice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

from ..config import INTEGRATIONS
from .base import ToolError, tool


def _local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(INTEGRATIONS.timezone))
    except Exception:  # noqa: BLE001
        return datetime.now().astimezone()


def _ordinal(day: int) -> str:
    """1 -> 1st, 22 -> 22nd. These strings get spoken aloud, and "the 28 of
    August" is audibly wrong in a way that undercuts the whole illusion."""
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }".replace(" ", "")


def _spoken(now: datetime) -> str:
    hour = now.strftime("%I").lstrip("0") or "12"
    return (
        f"{now.strftime('%A')} the {_ordinal(now.day)} of {now.strftime('%B')}, "
        f"{hour}:{now.strftime('%M %p')}"
    )


@tool(group="core", always_available=True)
def get_time(timezone: str = "") -> dict:
    """Get the current date and time.

    Call this before any reasoning about dates, deadlines, scheduling, or how
    long ago something happened. Do not guess what day it is.

    Args:
        timezone: IANA name such as Europe/London. Defaults to the user's
            configured local timezone.
    """
    if timezone:
        if timezone not in available_timezones():
            raise ToolError(
                f"unknown timezone: {timezone}",
                hint="use an IANA name such as Asia/Kolkata or America/New_York",
            )
        now = datetime.now(ZoneInfo(timezone))
    else:
        now = _local_now()

    return {
        "iso": now.isoformat(timespec="seconds"),
        "spoken": _spoken(now),
        "date": now.strftime("%Y-%m-%d"),
        "time_24h": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "timezone": str(now.tzinfo),
    }


@tool(group="core")
def date_math(days: int = 0, hours: int = 0, weeks: int = 0) -> dict:
    """Work out a date offset from now, forwards or backwards.

    Use for questions like "what date is three weeks on Friday" or "how far
    away is the deadline". Negative values look backwards.

    Args:
        days: Days to add. Negative goes back.
        hours: Hours to add.
        weeks: Weeks to add.
    """
    now = _local_now()
    target = now + timedelta(days=days, hours=hours, weeks=weeks)
    delta = target - now
    return {
        "result_iso": target.isoformat(timespec="seconds"),
        "result_spoken": f"{target.strftime('%A')} the {_ordinal(target.day)} of {target.strftime('%B')}",
        "weekday": target.strftime("%A"),
        "days_from_now": round(delta.total_seconds() / 86400, 2),
    }
