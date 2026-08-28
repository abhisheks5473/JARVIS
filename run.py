"""JARVIS.

    python run.py                  text and voice, HUD, scheduled jobs
    python run.py --text-only      no microphone, no speaker
    python run.py --once "..."     one turn, then exit
    python run.py --no-scheduler   no background jobs

Hotkeys, while running:
    ctrl+alt+j       push to talk
    ctrl+alt+space   shut it up mid-sentence
    ctrl+alt+q       kill switch

The kill switch is a key combination, not a voice command, on purpose. A
voice-activated stop is exactly the thing that fails when you most need it.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config  # noqa: E402
from jarvis.agent import Agent  # noqa: E402
from jarvis.hud.display import make_hud  # noqa: E402
from jarvis.logging_setup import log  # noqa: E402
from jarvis.memory.store import memory  # noqa: E402
from jarvis.quota import governor  # noqa: E402
from jarvis.security.approval import ApprovalGate  # noqa: E402
from jarvis.security.trash import list_trash  # noqa: E402
from jarvis.tools import catalogue, registry, use_profile  # noqa: E402

HELP = """\
  /status      quota, security state, session totals
  /quota       where today's requests actually went
  /memory      what it knows about you
  /tools       the tool catalogue and the active loadout
  /profile X   switch tool set: default, desk, research, dev, briefing, minimal
  /taint       why the conversation is flagged, and what that changed
  /clear       forget the taint flag (only you can do this, never the model)
  /trash       what is recoverable, and for how long
  /queue       actions a background job wanted but could not take
  /voice       toggle speaking replies aloud
  /jobs        scheduled background jobs
  /help        this
  /quit        save the session to memory and exit\
"""


class Session:
    """Wires the agent, the HUD, voice and the scheduler together."""

    def __init__(self, text_only: bool = False, hud_enabled: bool = True) -> None:
        self.hud = make_hud(config.INTEGRATIONS.assistant_name, hud_enabled)
        self.speak_replies = not text_only
        self.text_only = text_only
        self.running = True
        self.busy = threading.Lock()

        # The gate asks through the HUD. Injecting it this way is what lets a
        # scheduled job construct a gate with no prompter at all, making
        # self-approval structurally impossible rather than merely forbidden.
        self.agent = Agent(
            gate=ApprovalGate(prompter=self.hud.approval),
            on_event=self.hud.event,
        )

        self.ears = None
        self.speaker = None
        self.hotkeys = None
        self.nerves = None

    # ------------------------------------------------------------ startup
    def banner(self) -> None:
        snapshot = governor.snapshot()
        self.hud.banner(
            [
                f"{len(registry)} tools · {memory.count()} remembered facts",
                f"quota {snapshot.rpd_used}/{snapshot.rpd_limit} today "
                f"({snapshot.mode.value}), resets in {snapshot.resets_in_s / 3600:.1f}h",
                f"model {config.Models.FAST.id} / {config.Models.SMART.id}",
                "",
                "Type a message, or /help for commands.",
            ]
        )
        if not config.api_key_present():
            self.hud.note(
                "No GEMINI_API_KEY in .env -- only local commands will work. "
                "Get one free at aistudio.google.com/apikey"
            )

    def start_voice(self) -> None:
        if self.text_only:
            return

        from jarvis.voice.stt import ears
        from jarvis.voice.tts import speaker

        self.ears = ears
        self.speaker = speaker

        floor = ears.calibrate()
        self.hud.note(f"microphone calibrated, noise floor {floor:.4f}")

        from jarvis.voice.hotkey import default_hotkeys

        self.hotkeys = default_hotkeys(
            on_talk=self.voice_turn,
            on_kill=self.kill,
            on_interrupt=self.interrupt,
        )
        if self.hotkeys.start():
            self.hud.note(
                f"push to talk: {config.VOICE.hotkey} · "
                "interrupt: ctrl+alt+space · kill: ctrl+alt+q"
            )
        else:
            self.hud.note(f"hotkeys unavailable ({self.hotkeys.last_error})")

    def start_scheduler(self) -> None:
        from jarvis.triggers.scheduler import Nerves

        self.nerves = Nerves(notify=self.announce)
        if self.nerves.start():
            self.hud.note(f"background jobs: {', '.join(self.nerves.jobs())}")
        else:
            self.hud.note(f"scheduler unavailable ({self.nerves.last_error})")

    # ------------------------------------------------------------ speaking
    def say(self, text: str) -> None:
        self.hud.reply(text)
        if self.speak_replies and self.speaker is not None:
            self.speaker.speak(text, blocking=False)

    def announce(self, text: str) -> None:
        """Output from a background job, which arrives unprompted."""
        self.hud.note(f"[background] {text}")
        if self.speak_replies and self.speaker is not None:
            self.speaker.speak(text, blocking=False)

    def interrupt(self) -> None:
        if self.speaker is not None:
            self.speaker.stop()
        if self.ears is not None:
            self.ears.stop()

    def kill(self) -> None:
        self.hud.note("kill switch")
        self.interrupt()
        self.running = False

    # ------------------------------------------------------------ turns
    def voice_turn(self) -> None:
        """Triggered by the push-to-talk hotkey, off the main thread."""
        if not self.busy.acquire(blocking=False):
            return
        try:
            if self.speaker is not None and self.speaker.is_speaking:
                # Pressing talk while it is talking means "stop and listen".
                self.speaker.stop()
                time.sleep(0.15)

            self.hud.note("listening...")
            heard = self.ears.listen()
            if not heard:
                self.hud.note("nothing heard")
                return

            self.hud.user_echo(heard)
            if not self.handle(heard):
                self.running = False
        finally:
            self.busy.release()

    def handle(self, text: str) -> bool:
        """Returns False when the session should end."""
        text = text.strip()
        if not text:
            return True

        if text.startswith("/"):
            return self.command(text)

        report = self.agent.run(text)
        self.say(report.reply)

        if report.degraded:
            self.hud.note("(answered on the cheap model to save quota)")
        return True

    # ------------------------------------------------------------ commands
    def command(self, raw: str) -> bool:
        parts = raw[1:].split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""

        if name in ("quit", "exit", "q"):
            return False

        if name == "help":
            print(HELP)

        elif name == "status":
            self.hud.status(
                governor.snapshot(),
                self.agent.ledger.level,
                extra=(
                    f"{self.agent.turns} turns this session · "
                    f"{len(self.agent.history)} history entries"
                ),
            )

        elif name == "quota":
            rows = governor.by_kind()
            if not rows:
                self.hud.note("no API calls yet today")
            for row in rows:
                self.hud.note(
                    f"{row['kind']:<9} {row['requests']:>4} requests  "
                    f"{row['tokens']:>8,} tokens"
                )
            for day in governor.daily_report(5):
                self.hud.note(
                    f"{day['day']}  {day['requests']:>4} req  "
                    f"{day['tokens']:>8,} tok  {day['failures']} failures"
                )

        elif name == "memory":
            facts = memory.all_facts(30)
            if not facts:
                self.hud.note("nothing remembered yet")
            for fact in facts:
                self.hud.note(f"[{fact.id}] ({fact.category}) {fact.fact}")

        elif name == "tools":
            for group, names in catalogue().items():
                self.hud.note(f"{group:<9} {', '.join(names)}")
            active = sorted(s.name for s in registry.active())
            self.hud.note(f"active now ({len(active)}): {', '.join(active)}")

        elif name == "profile":
            active = use_profile(argument.strip() or "default")
            self.hud.note(f"{len(active)} tools: {', '.join(active)}")

        elif name == "taint":
            self.hud.note(self.agent.ledger.explain())
            for event in self.agent.ledger.events[-6:]:
                self.hud.note(f"  {event.tool}: {event.level.name} -- {event.summary}")

        elif name == "clear":
            self.agent.ledger.clear()
            self.hud.note("taint cleared; destructive actions back to normal gating")

        elif name == "trash":
            entries = list_trash()
            if not entries:
                self.hud.note("trash is empty")
            for entry in entries[:20]:
                self.hud.note(
                    f"[{entry.entry_id}] {entry.original_path} "
                    f"({entry.expires_in_days:.0f} days left)"
                )

        elif name == "queue":
            pending = self.agent.gate.pending()
            if not pending:
                self.hud.note("nothing queued")
            for action in pending:
                self.hud.note(f"{action.tool}({action.arguments}) -- {action.reason}")

        elif name == "voice":
            self.speak_replies = not self.speak_replies
            self.hud.note(f"speaking replies: {self.speak_replies}")

        elif name == "jobs":
            jobs = self.nerves.jobs() if self.nerves else []
            self.hud.note(", ".join(jobs) if jobs else "no scheduled jobs")

        else:
            self.hud.note(f"unknown command: /{name}. Try /help")

        return True

    # ------------------------------------------------------------ loop
    def repl(self) -> None:
        while self.running:
            try:
                text = input("\nyou> ")
            except (EOFError, KeyboardInterrupt):
                break

            if not self.busy.acquire(blocking=False):
                self.hud.note("busy with a voice turn, one moment")
                continue
            try:
                if not self.handle(text):
                    break
            finally:
                self.busy.release()

    def shutdown(self) -> None:
        self.hud.note("saving session to memory...")
        self.interrupt()
        if self.hotkeys:
            self.hotkeys.stop()
        if self.nerves:
            self.nerves.stop()
        try:
            self.agent.end_session()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save session: %s", exc)
        self.hud.note("goodbye")


def main() -> int:
    parser = argparse.ArgumentParser(description="JARVIS")
    parser.add_argument(
        "--text-only", action="store_true", help="no microphone or speaker"
    )
    parser.add_argument("--no-hud", action="store_true", help="plain output")
    parser.add_argument(
        "--no-scheduler", action="store_true", help="no background jobs"
    )
    parser.add_argument("--once", metavar="PROMPT", help="run one turn and exit")
    parser.add_argument("--profile", default="default", help="starting tool set")
    args = parser.parse_args()

    use_profile(args.profile)
    session = Session(text_only=args.text_only, hud_enabled=not args.no_hud)

    if args.once:
        report = session.agent.run(args.once)
        print(report.reply)
        return 1 if report.error else 0

    session.banner()
    session.start_voice()
    if not args.no_scheduler:
        session.start_scheduler()

    try:
        session.repl()
    finally:
        session.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
