"""What every slash command is, in one place.

The list used to exist twice: as the if/elif chain that runs them, and as a
hardcoded help string that named them. Those drift -- `/key` and `/wake test`
both shipped before the help line mentioned either. The panel, the help line
and the descriptions now all read from here, so a command added once is
described everywhere.

This describes commands; `window._command` still runs them. Keeping the two
apart means anything can read this -- the panel, the help text, a test --
without dragging the whole window in behind it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str            # "/quota", exactly as it would be typed
    summary: str         # one line, for the list
    detail: str          # what it actually does, shown before running
    argument: str = ""   # placeholder when it takes one, "" when it does not
    example: str = ""    # a filled-in example, for the argument case
    caution: str = ""    # shown in warning colour when it changes something

    @property
    def takes_argument(self) -> bool:
        return bool(self.argument)


COMMANDS: tuple[Command, ...] = (
    Command(
        name="/quota",
        summary="Where today's requests actually went",
        detail=(
            "Breaks the day's usage down by what spent it -- agent turns, "
            "vision calls, summaries -- so a surprising number has somewhere "
            "to be traced to. Reads a local ledger; no request is made."
        ),
    ),
    Command(
        name="/memory",
        summary="What it knows about you",
        detail=(
            "Lists the facts it has stored, with their ids. These are injected "
            "into every conversation, so this is where to look if it seems to "
            "believe something odd about you."
        ),
    ),
    Command(
        name="/key",
        summary="Change model provider or API key",
        detail=(
            "Opens the provider panel. Switch between Gemini, OpenAI, "
            "Anthropic, Groq and the rest, or paste a new key. It takes effect "
            "immediately and the conversation carries on."
        ),
    ),
    Command(
        name="/route",
        summary="Which provider gets which kind of question",
        detail=(
            "Shows every provider you have a key for, the model it uses, and "
            "the kinds of work it is tagged for. Questions are sorted into "
            "those kinds and sent to whichever provider claims them -- and it "
            "switches again mid-task if the subject changes. Tag them with "
            "the Key button."
        ),
    ),
    Command(
        name="/wake",
        summary="Record a wake word in your own voice",
        detail=(
            "Records the phrase five times and learns it from your voice, so "
            "it answers to you rather than to the television. About twenty "
            "seconds. Recording again replaces whatever is there now."
        ),
        argument="phrase to record",
        example="/wake jarvis",
        caution="Replaces any wake word already recorded.",
    ),
    Command(
        name="/wake test",
        summary="Show what the wake word has been scoring",
        detail=(
            "Lists the last ten things it heard, what each scored, and the "
            "threshold they were compared against. Anything under the "
            "threshold woke it. This is how to tell whether it is firing too "
            "eagerly or not enough, rather than guessing at it."
        ),
    ),
    Command(
        name="/wake off",
        summary="Forget the wake word and stop listening",
        detail=(
            "Deletes the recorded phrase and stops the microphone listening in "
            "the background. You can record a new one whenever you like."
        ),
        caution="Deletes the recording. You would have to record it again.",
    ),
    Command(
        name="/voice",
        summary="Speak replies aloud, or don't",
        detail=(
            "Toggles whether answers are spoken as well as written. Speech is "
            "generated on this machine, so it costs nothing and works offline."
        ),
    ),
    Command(
        name="/clear",
        summary="Clear the transcript and the taint flag",
        detail=(
            "Empties the window and clears the security flag set when "
            "something untrusted has been read. Only you can clear that flag "
            "-- the model cannot, which is the entire point of it."
        ),
        caution="Clears the injection flag. Do this only if you know why it was set.",
    ),
    Command(
        name="/quit",
        summary="Shut down properly",
        detail=(
            "Stops the background jobs, the wake word and the hotkeys, saves "
            "the conversation to memory, then closes. Better than closing the "
            "window, which on Windows only hides it to the tray."
        ),
        caution="Closes JARVIS.",
    ),
)


def help_line() -> str:
    """The one-line list shown when an unknown command is typed."""
    names = " ".join(c.name for c in COMMANDS if " " not in c.name)
    return f"Commands: {names}"
