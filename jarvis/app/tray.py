"""System tray presence.

An assistant that dies when you close its window is not an assistant, it is a
program you keep reopening. The tray icon is what makes closing the window
mean "get out of the way" rather than "stop existing" -- the scheduled jobs
keep running and push-to-talk still works from anywhere.

pystray runs its own loop on its own thread, so every menu action is bounced
back onto the Tk thread with `after(0, ...)`. Calling into Tk from here
directly would work most of the time and crash the rest, which is the worst
kind of bug to ship.
"""
from __future__ import annotations

import threading

from .. import config


def _icon_image():
    """Draw the icon rather than shipping a binary asset.

    Sixty-four pixels of Pillow beats a .ico in the repository: nothing to
    lose, nothing to keep in sync, and it works from a PyInstaller bundle
    without data-file plumbing.
    """
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (13, 17, 23, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (2, 2, size - 3, size - 3),
        fill=(13, 17, 23, 255),
        outline=(88, 166, 255, 255),
        width=3,
    )
    draw.ellipse((20, 20, size - 21, size - 21), fill=(88, 166, 255, 255))
    return image


class Tray:
    """Tray icon wrapper. Degrades to nothing if pystray is unavailable."""

    def __init__(self, window) -> None:
        self.window = window
        self.icon = None
        self.running = False
        self.last_error = ""

    def start(self) -> bool:
        try:
            import pystray
        except ImportError as exc:
            self.last_error = f"pystray is not installed ({exc})"
            return False

        try:
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Show",
                    lambda *_: self.window.after(0, self.window.show_window),
                    default=True,
                ),
                pystray.MenuItem(
                    "Talk",
                    lambda *_: self.window.after(0, self.window.bridge.listen),
                ),
                pystray.MenuItem(
                    "Stop speaking",
                    lambda *_: self.window.after(0, self.window.bridge.stop_speaking),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Quit", lambda *_: self.window.after(0, self.window.quit_app)
                ),
            )
            self.icon = pystray.Icon(
                "jarvis", _icon_image(), config.INTEGRATIONS.assistant_name, menu
            )
            threading.Thread(target=self.icon.run, daemon=True).start()
            self.running = True
        except Exception as exc:  # noqa: BLE001 - no tray is not fatal
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    def notify(self, title: str, message: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception:  # noqa: BLE001 - notifications are optional
                pass

    def stop(self) -> None:
        self.running = False
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
