"""The seam between a blocking agent and a GUI that must never block.

An agent turn takes seconds and holds its thread the whole time. Tk is not
thread-safe and repaints on one thread only. Those two facts decide the design:

  * the agent runs on a worker thread,
  * everything it wants to say arrives as an event on a queue,
  * the GUI drains that queue on a timer and touches widgets only from the
    main thread.

The awkward case is approval. The gate calls a prompter *from the worker
thread* and needs a yes or no before it can continue, but the dialog has to be
built on the GUI thread. `GuiApproval` handles that handoff: it parks the
worker on an Event while the GUI raises the dialog, then hands the answer back.
It fails closed on timeout, because a prompt nobody answered is not consent.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Event:
    """One thing that happened, on its way to the GUI."""

    kind: str
    data: dict = field(default_factory=dict)


class GuiApproval:
    """Cross-thread approval prompt.

    The worker blocks here; the GUI answers. With routine prompts switched
    off this fires only for the taint guard, which makes it rare and
    important rather than background noise.
    """

    def __init__(self, emit: Callable[[Event], None], timeout: float = 300.0) -> None:
        self._emit = emit
        self._timeout = timeout
        self._answer: bool | None = None
        self._answered = threading.Event()
        self._lock = threading.Lock()

    def ask(self, tool: str, arguments: dict, reason: str) -> bool:
        """Called on the worker thread. Blocks until the GUI answers."""
        with self._lock:
            self._answer = None
            self._answered.clear()
            self._emit(
                Event(
                    "approval_request",
                    {"tool": tool, "arguments": arguments, "reason": reason},
                )
            )
            if not self._answered.wait(self._timeout):
                # Nobody was there. Silence is not consent.
                self._emit(Event("approval_timeout", {"tool": tool}))
                return False
            return bool(self._answer)

    def answer(self, approved: bool) -> None:
        """Called on the GUI thread when the user clicks."""
        self._answer = approved
        self._answered.set()


class AgentBridge:
    """Owns the agent and the worker thread; speaks only in events."""

    def __init__(self) -> None:
        self.events: queue.Queue[Event] = queue.Queue()
        self.approval = GuiApproval(self.events.put)
        self._agent = None
        self._worker: threading.Thread | None = None
        self._busy = threading.Event()
        self.speak_replies = True
        self._speaker = None
        self._ears = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Build the agent. Deferred so the window paints before this runs."""
        from ..agent import Agent
        from ..security.approval import ApprovalGate

        self._agent = Agent(
            gate=ApprovalGate(prompter=self.approval.ask),
            on_event=lambda kind, data: self.events.put(Event(kind, data)),
        )
        self.events.put(Event("ready", {}))

    @property
    def agent(self):
        return self._agent

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    # ------------------------------------------------------------ turns
    def send(self, text: str) -> bool:
        """Queue a turn. Returns False if one is already running."""
        if self._busy.is_set() or self._agent is None:
            return False
        self._busy.set()
        self._worker = threading.Thread(
            target=self._run_turn, args=(text,), daemon=True
        )
        self._worker.start()
        return True

    def _run_turn(self, text: str) -> None:
        started = time.time()
        try:
            report = self._agent.run(text)
            self.events.put(
                Event(
                    "turn_done",
                    {
                        "reply": report.reply,
                        "tools": list(report.tool_calls),
                        "denied": list(report.denied),
                        "tokens": report.tokens,
                        "model": report.model,
                        "taint": report.taint_level,
                        "error": report.error,
                        "seconds": round(time.time() - started, 1),
                    },
                )
            )
            if self.speak_replies and report.reply:
                self._speak(report.reply)
        except Exception as exc:  # noqa: BLE001 - a crash must reach the window
            self.events.put(Event("fatal", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            self._busy.clear()

    # ------------------------------------------------------------ voice
    def _speak(self, text: str) -> None:
        try:
            if self._speaker is None:
                from ..voice.tts import speaker

                self._speaker = speaker
            self._speaker.speak(text, blocking=False)
        except Exception:  # noqa: BLE001 - no audio device is not fatal
            pass

    def stop_speaking(self) -> None:
        if self._speaker is not None:
            try:
                self._speaker.stop()
            except Exception:  # noqa: BLE001
                pass

    def listen(self) -> None:
        """Record from the microphone on a worker thread, then run the turn."""
        if self._busy.is_set():
            return
        threading.Thread(target=self._listen_worker, daemon=True).start()

    def _listen_worker(self) -> None:
        try:
            if self._ears is None:
                from ..voice.stt import ears

                self._ears = ears
                self.events.put(Event("status", {"text": "calibrating microphone"}))
                self._ears.calibrate()

            self.stop_speaking()
            self.events.put(Event("listening", {"on": True}))
            heard = self._ears.listen()
            self.events.put(Event("listening", {"on": False}))

            if not heard:
                self.events.put(Event("status", {"text": "nothing heard"}))
                return
            self.events.put(Event("heard", {"text": heard}))
            self.send(heard)
        except Exception as exc:  # noqa: BLE001
            self.events.put(Event("listening", {"on": False}))
            self.events.put(
                Event("status", {"text": f"microphone problem: {type(exc).__name__}"})
            )

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        self.stop_speaking()
        if self._agent is not None:
            try:
                self._agent.end_session()
            except Exception:  # noqa: BLE001 - never block quitting
                pass

    def drain(self, limit: int = 60) -> list[Event]:
        """Pull pending events. Called from the GUI thread on a timer."""
        out: list[Event] = []
        for _ in range(limit):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out
