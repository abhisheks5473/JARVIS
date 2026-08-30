"""Nerves: the things that happen when you are not talking to it.

This is what makes an assistant feel like JARVIS rather than Siri. It is also
where an agent can quietly burn a day's quota at 4am, so every job here runs
under a strict budget.

The pattern that works, and the one this enforces:

    cheap deterministic code decides *whether* to think;
    the model decides *what to do*.

Never poll by asking the model "is anything interesting happening?" every
minute. The file watcher compares mtimes in Python; only a real change wakes
the model.

Every autonomous run gets: an unattended agent (no approval channel, so it
structurally cannot take a gated action), a step cap, a wall-clock timeout,
and a quota check that fails closed. Actions it wanted but could not take are
queued for you rather than skipped silently.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from .. import config
from ..logging_setup import log, transcript
from ..prompts import BRIEFING_PROMPT
from ..quota import governor
from ..security.approval import ApprovalGate
from ..security.trash import empty_expired


_ACTIVE_CALL_WATCHER = None


@dataclass
class JobResult:
    name: str
    ran: bool
    output: str = ""
    skipped_reason: str = ""
    queued_actions: list[str] = field(default_factory=list)


def _unattended_agent():
    """An agent that cannot approve anything, by construction.

    The gate is built with `prompter=None`, so any destructive action is
    queued rather than performed. This is a structural guarantee, not a
    policy a clever prompt could talk its way around.
    """
    from ..agent import Agent

    return Agent(gate=ApprovalGate(prompter=None), interactive=False)


def run_autonomous(name: str, prompt: str, profile: str = "default") -> JobResult:
    """Run one unattended turn under a budget."""
    verdict = governor.check(interactive=False)
    if not verdict.allowed:
        log.info("skipping job %s: %s", name, verdict.reason)
        return JobResult(name, ran=False, skipped_reason=verdict.reason)

    agent = _unattended_agent()
    agent.forced_profile = profile

    started = time.time()
    report = agent.run(prompt)
    queued = [action.tool for action in agent.gate.pending()]
    elapsed = time.time() - started

    transcript.log_event(
        "autonomous_job",
        job=name,
        tokens=report.tokens,
        api_calls=report.api_calls,
        seconds=round(elapsed, 1),
        queued=",".join(queued),
        error=report.error,
    )
    log.info("job %s finished in %.1fs (%d tokens)", name, elapsed, report.tokens)

    return JobResult(name, ran=True, output=report.reply, queued_actions=queued)


# ---------------------------------------------------------------- jobs
def morning_briefing(notify: Callable[[str], None] | None = None) -> JobResult:
    """Check the calendar and the day, and say something short about it."""
    result = run_autonomous("morning_briefing", BRIEFING_PROMPT, profile="briefing")
    if result.ran and result.output and notify:
        notify(result.output)
    return result


def empty_trash_job() -> JobResult:
    """Purge trash older than the retention window.

    Deterministic and free: no model is involved in deciding what to delete,
    only a date comparison. That is the whole point of the design.
    """
    removed = empty_expired()
    if removed:
        log.info("purged %d expired trash entries", len(removed))
    return JobResult(
        "empty_trash", ran=True, output=f"purged {len(removed)} expired items"
    )


class DownloadsWatcher:
    """Notice new files in a folder without asking the model anything.

    Threshold triggering: names are compared in Python, and the model is only
    woken when something has actually appeared.
    """

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or (Path.home() / "Downloads")
        self._seen: set[str] = set()
        self._primed = False

    def poll(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        try:
            current = {
                p.name
                for p in self.folder.iterdir()
                if p.is_file()
                and not p.name.endswith((".tmp", ".crdownload", ".part"))
            }
        except OSError:
            return []

        if not self._primed:
            # The first run records a baseline; announcing every existing file
            # once at startup would be noise, not a feature.
            self._seen = current
            self._primed = True
            return []

        new = sorted(current - self._seen)
        self._seen = current
        return new


class Nerves:
    """APScheduler wrapper holding every trigger."""

    def __init__(self, notify: Callable[[str], None] | None = None) -> None:
        self.notify = notify
        self.scheduler = None
        self.watcher = DownloadsWatcher()
        self.calls = CallWatcher(notify=notify)
        self.last_error = ""

    def start(self, briefing_hour: int = 7, briefing_minute: int = 0) -> bool:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError as exc:
            self.last_error = f"APScheduler is not installed ({exc})"
            return False

        self.scheduler = BackgroundScheduler(timezone=config.INTEGRATIONS.timezone)

        self.scheduler.add_job(
            lambda: morning_briefing(self.notify),
            CronTrigger(hour=briefing_hour, minute=briefing_minute),
            id="morning_briefing",
            replace_existing=True,
        )
        self.scheduler.add_job(
            empty_trash_job,
            CronTrigger(hour=3, minute=30),
            id="empty_trash",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._check_downloads,
            IntervalTrigger(minutes=5),
            id="downloads_watch",
            replace_existing=True,
        )
        # Two seconds, because a call rings for about thirty and a missed
        # poll is a missed call. It costs nothing: the check is a window
        # enumeration, and it returns immediately unless a rule is armed.
        self.scheduler.add_job(
            self.calls.poll,
            IntervalTrigger(seconds=CallWatcher.POLL_SECONDS),
            id="whatsapp_calls",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._health_check,
            IntervalTrigger(hours=6),
            id="health_check",
            replace_existing=True,
        )

        try:
            self.scheduler.start()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"could not start the scheduler: {exc}"
            return False

        # Published only once the jobs are genuinely running. Setting this in
        # __init__ meant auto_decline_calls could accept a rule and report
        # success while nothing was polling for calls -- the rule was armed
        # and simply never fired.
        globals()["_ACTIVE_CALL_WATCHER"] = self.calls
        return True

    def _check_downloads(self) -> None:
        new_files = self.watcher.poll()
        if not new_files:
            return
        # Deliberately no model call. Noticing a file is free; reasoning about
        # it is not, and most downloads need no reasoning at all.
        message = (
            f"{len(new_files)} new file(s) in Downloads: {', '.join(new_files[:4])}"
        )
        log.info(message)
        if self.notify:
            self.notify(message)

    def _health_check(self) -> None:
        """Agents fail silently; this is the thing that notices.

        A rate limit hits, a retry loop swallows it, and you do not find out
        for two days. Six-hourly is often enough to catch that and rare enough
        to cost nothing.
        """
        snapshot = governor.snapshot()
        report = governor.daily_report(days=1)
        failures = report[0]["failures"] if report else 0
        transcript.log_event(
            "health",
            mode=snapshot.mode.value,
            rpd=f"{snapshot.rpd_used}/{snapshot.rpd_limit}",
            tokens_today=snapshot.tokens_today,
            failures_today=failures,
        )
        if failures > 10 and self.notify:
            self.notify(f"{failures} API failures today. Something is wrong.")

    def jobs(self) -> list[str]:
        if self.scheduler is None:
            return []
        return [job.id for job in self.scheduler.get_jobs()]

    def stop(self) -> None:
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass


class CallWatcher:
    """Decline WhatsApp calls from named people, and reply for you.

    Deterministic code decides *whether* anything is happening -- it polls for
    a call window, which is free -- and only a name match triggers action. The
    model is never asked "is someone calling?", because that would spend quota
    every two seconds to learn "no".

    Rules are (name, reply) pairs. A name matches when it appears in the call
    window's title, case-insensitively, so "mum" catches "Mum" and "Mummy".
    The empty name "*" matches everyone, which is how "decline all calls" is
    expressed.

    A one-shot guard stops a repeat: WhatsApp rings for many seconds, and
    without it the same call would be declined and replied to over and over
    while it was still ringing.
    """

    POLL_SECONDS = 2

    def __init__(self, notify: Callable[[str], None] | None = None) -> None:
        self.rules: list[tuple[str, str]] = []
        self.notify = notify
        self.enabled = False
        self._handled: str = ""
        self._handled_at: float = 0.0
        self.declined: list[dict] = []

    def add_rule(self, name: str, reply: str = "") -> None:
        self.rules.append((name.strip().lower(), reply.strip()))
        self.enabled = True

    def clear_rules(self) -> None:
        self.rules.clear()
        self.enabled = False

    def _matches(self, title: str) -> tuple[bool, str]:
        lowered = title.lower()
        for name, reply in self.rules:
            if name in ("*", "all", "everyone") or (name and name in lowered):
                return True, reply
        return False, ""

    def poll(self) -> None:
        if not self.enabled or not self.rules:
            return
        try:
            from ..tools.whatsapp import decline_whatsapp_call, find_incoming_call, send_whatsapp
        except Exception:  # noqa: BLE001 - missing libs must not kill the scheduler
            return

        try:
            call = find_incoming_call()
        except Exception:  # noqa: BLE001
            return
        if call is None:
            self._handled = ""       # the call ended; allow the next one
            return

        # A name the rules can be matched against. An unnamed call still
        # matches "*", but never matches a rule naming somebody.
        title = call.get("caller") or ""
        # One action per ringing call, not one per poll. The key is never
        # empty, because "" is what _handled is reset to between calls -- an
        # unnamed call would otherwise look like one already dealt with, and
        # never be declined at all.
        key = title or "(unnamed call)"
        if key == self._handled and time.time() - self._handled_at < 120:
            return

        matched, reply = self._matches(title)
        if not matched:
            return

        self._handled, self._handled_at = key, time.time()
        record = {"caller": title, "at": time.time(), "replied": False}

        try:
            decline_whatsapp_call()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not decline call from %s: %s", title, exc)

        if reply:
            try:
                time.sleep(1.2)      # let the call window close first
                send_whatsapp(contact=title, message=reply)
                record["replied"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("declined %s but could not reply: %s", title, exc)

        self.declined.append(record)
        del self.declined[:-50]
        transcript.log_event("call_declined", caller=title, replied=record["replied"])
        if self.notify:
            self.notify(
                f"Declined a WhatsApp call from {title}"
                + (" and replied." if record["replied"] else ".")
            )
