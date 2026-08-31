"""The core: a glowing thing that moves with the voice in the room.

It pulses to the **actual audio**, not to a timer. The microphone loop and the
speech synthesiser both already measured loudness for their own purposes -- one
to know when you stopped talking, the other not using it at all -- so both now
publish it and this reads it. A fake animation on a timer is convincing for
about four seconds, until you stop talking and it carries on.

**Tk has no alpha channel**, so the glow is faked the way glows were faked
before alpha existed: concentric rings, each a step closer to the background
colour. Against a fixed dark background that is indistinguishable from a real
gradient, and it costs a dozen ovals rather than a compositing pass.

**Redrawing is one loop** at about thirty frames a second, moving existing
items rather than deleting and recreating them. Tk redraws the whole canvas on
any change, so animating each ring on its own timer would mean a dozen full
redraws per frame for one frame of movement.
"""
from __future__ import annotations

import math
import tkinter as tk

BG = "#0d1117"

STATE_COLOURS = {
    "idle": "#2f5d8a",        # quiet blue, breathing
    "listening": "#3fb950",   # green: your voice going in
    "speaking": "#58a6ff",    # accent blue: its voice coming out
    "thinking": "#d29922",    # amber: working, not hearing
}

RINGS = 7
FPS_MS = 33


def _blend(colour: str, other: str, amount: float) -> str:
    """Mix two #rrggbb colours. amount=0 gives the first, 1 gives the second."""
    amount = max(0.0, min(1.0, amount))
    a = (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
    b = (int(other[1:3], 16), int(other[3:5], 16), int(other[5:7], 16))
    return "#%02x%02x%02x" % tuple(
        int(round(x + (y - x) * amount)) for x, y in zip(a, b)
    )


class Orb(tk.Canvas):
    """A pulsing core. Feed it a level; it does the rest."""

    def __init__(self, master, size: int = 170, background: str = BG):
        super().__init__(
            master, width=size, height=size,
            bg=background, highlightthickness=0, bd=0,
        )
        self.size = size
        self.background = background
        self.state_name = "idle"
        self._level = 0.0        # smoothed, and what actually gets drawn
        self._target = 0.0       # raw, as last reported
        self._phase = 0.0        # idle breathing
        self._spin = 0.0         # orbit rotation
        self._running = True

        centre = size / 2
        self._rings = [
            self.create_oval(centre, centre, centre, centre, outline="", width=2)
            for _ in range(RINGS)
        ]
        self._orbits = [
            self.create_arc(
                centre, centre, centre, centre, start=0, extent=110,
                style=tk.ARC, outline="", width=2,
            )
            for _ in range(2)
        ]
        self._core = self.create_oval(centre, centre, centre, centre, outline="")

        self._tick()

    # ---------------------------------------------------------------- inputs
    def set_state(self, name: str) -> None:
        if name in STATE_COLOURS:
            self.state_name = name

    def set_level(self, level: float) -> None:
        """Report loudness, 0..1. Anything outside is clamped, not trusted."""
        try:
            self._target = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self._target = 0.0

    def stop(self) -> None:
        """Stop animating, for shutdown."""
        self._running = False

    # ----------------------------------------------------------------- frame
    def _tick(self) -> None:
        if not self._running:
            return

        # Rise fast, fall slow. Following the audio exactly reads as a flicker,
        # because speech is full of gaps a few milliseconds long that the ear
        # ignores and the eye does not.
        if self._target > self._level:
            self._level += (self._target - self._level) * 0.55
        else:
            self._level += (self._target - self._level) * 0.12

        self._phase += 0.045
        self._spin += 1.4 + self._level * 9.0

        try:
            self._draw()
        except tk.TclError:
            return          # the window went away mid-frame

        self.after(FPS_MS, self._tick)

    def _draw(self) -> None:
        centre = self.size / 2
        colour = STATE_COLOURS.get(self.state_name, STATE_COLOURS["idle"])

        # A slow breath underneath, so it is alive when the room is silent.
        breath = 0.5 + 0.5 * math.sin(self._phase)
        energy = min(1.0, self._level * 1.6)
        base = self.size * (0.115 + 0.02 * breath + 0.085 * energy)

        for index, ring in enumerate(self._rings):
            radius = base * (1.0 + index * (0.20 + 0.06 * energy))
            # Further out means closer to the background: a gradient without
            # a gradient.
            fade = (index + 1) / (RINGS + 1)
            self.coords(
                ring, centre - radius, centre - radius,
                centre + radius, centre + radius,
            )
            self.itemconfigure(
                ring,
                outline=_blend(colour, self.background, 0.30 + fade * 0.62),
                width=2 if index < 2 else 1,
            )

        orbit_radius = base * (1.85 + 0.25 * energy)
        for index, arc in enumerate(self._orbits):
            self.coords(
                arc, centre - orbit_radius, centre - orbit_radius,
                centre + orbit_radius, centre + orbit_radius,
            )
            self.itemconfigure(
                arc,
                start=(self._spin * (1 if index == 0 else -0.7)) % 360 + index * 180,
                outline=_blend(colour, self.background, 0.45),
            )

        core = base * (0.42 + 0.30 * energy)
        self.coords(
            self._core, centre - core, centre - core, centre + core, centre + core,
        )
        # The centre whitens as it gets loud, which is what makes it read as
        # brightness rather than as a circle that changed size.
        self.itemconfigure(
            self._core, fill=_blend(colour, "#ffffff", 0.25 + 0.55 * energy)
        )
