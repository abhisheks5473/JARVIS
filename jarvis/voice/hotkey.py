"""Push-to-talk, and the kill switch.

Push-to-talk on a hotkey is boring and works perfectly. A wake-word engine is
more fun and occasionally triggers on your television. This is the boring one,
which is the right default: no idle CPU, no false triggers, and nothing
listening until you say so.

The kill switch is deliberately a key combination rather than a voice command.
A voice-activated stop is exactly the thing that fails when you most need it:
when the assistant is talking over you, when the room is loud, or when
something has gone wrong with audio in the first place.
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable

from .. import config


def _parse(combo: str) -> tuple[set[str], str]:
    """Turn "ctrl+alt+j" into ({ctrl, alt}, "j")."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return set(), "j"
    return set(parts[:-1]), parts[-1]


class Hotkeys:
    """Global hotkey listener built on pynput.

    pynput is used rather than `keyboard` because it does not need
    administrator rights on Windows, which matters for something you want
    running at login.
    """

    def __init__(self) -> None:
        self._listener = None
        self._pressed: set[str] = set()
        self._bindings: list[tuple[set[str], str, Callable[[], None]]] = []
        self._lock = threading.Lock()
        self.last_error = ""
        self.running = False

    def bind(self, combo: str, action: Callable[[], None]) -> None:
        modifiers, key = _parse(combo)
        self._bindings.append((modifiers, key, action))

    # ------------------------------------------------------------ matching
    @staticmethod
    def _modifier_name(key) -> str | None:
        name = getattr(key, "name", "") or ""
        if name.startswith("ctrl"):
            return "ctrl"
        if name.startswith("alt"):
            return "alt"
        if name.startswith("cmd"):
            return "cmd"
        if name.startswith("shift"):
            return "shift"
        return None

    @staticmethod
    def _char_of(key) -> str | None:
        char = getattr(key, "char", None)
        if char:
            return char.lower()
        name = getattr(key, "name", None)
        return name.lower() if name else None

    def _on_press(self, key) -> None:
        modifier = self._modifier_name(key)
        with self._lock:
            if modifier:
                self._pressed.add(modifier)
                return
            char = self._char_of(key)
            if not char:
                return
            active = set(self._pressed)

        for modifiers, target, action in self._bindings:
            if char == target and modifiers.issubset(active):
                # Run off the listener thread: a slow handler would otherwise
                # block every subsequent keystroke on the machine.
                threading.Thread(target=action, daemon=True).start()
                return

    def _on_release(self, key) -> None:
        modifier = self._modifier_name(key)
        if modifier:
            with self._lock:
                self._pressed.discard(modifier)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        # pynput's macOS backend maps key codes by calling the Text Services
        # Manager through ctypes, from its own listener thread. macOS 26
        # hardened those functions with dispatch_assert_queue, so being off
        # the main queue is no longer merely discouraged -- it aborts the
        # process. The crash report from a Mac shows it exactly:
        #
        #   Thread-1  ctypes -> TSMGetInputSourceProperty
        #             -> dispatch_assert_queue -> _dispatch_assert_queue_fail
        #             EXC_BREAKPOINT (SIGTRAP)
        #
        # There is no thread this can safely run on: Tk owns the main one.
        # The window binds the same shortcuts locally instead, which work
        # whenever JARVIS is focused and cannot take the process down.
        if sys.platform == "darwin":
            self.last_error = (
                "global hotkeys are unavailable on macOS: pynput's key "
                "mapping must run on the main thread, which the window owns"
            )
            return False

        try:
            from pynput import keyboard
        except ImportError as exc:
            self.last_error = f"pynput is not installed ({exc})"
            return False

        try:
            self._listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release
            )
            self._listener.daemon = True
            self._listener.start()
            self.running = True
        except Exception as exc:  # noqa: BLE001 - no display, no permission, etc.
            self.last_error = f"could not start the hotkey listener: {exc}"
            return False
        return True

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
        self.running = False


def default_hotkeys(
    on_talk: Callable[[], None],
    on_kill: Callable[[], None],
    on_interrupt: Callable[[], None] | None = None,
) -> Hotkeys:
    """Wire the standard bindings.

    talk      -- the configured hotkey, default ctrl+alt+j
    interrupt -- ctrl+alt+space, barge-in: shuts it up mid-sentence
    kill      -- ctrl+alt+q, stops everything
    """
    hotkeys = Hotkeys()
    hotkeys.bind(config.VOICE.hotkey, on_talk)
    hotkeys.bind("ctrl+alt+q", on_kill)
    if on_interrupt is not None:
        hotkeys.bind("ctrl+alt+space", on_interrupt)
    return hotkeys
