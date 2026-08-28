"""JARVIS, as an application.

    pythonw app.py      no console window
    python  app.py      with a console, for debugging

Or double-click JARVIS.vbs, which does the first for you.

Closing the window hides it to the tray; the assistant keeps running, the
scheduled jobs keep firing, and push-to-talk still works from any application.
Quit properly from the tray menu or with ctrl+alt+q.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _fatal(message: str) -> int:
    """Show a real dialog rather than dying into a console nobody can see.

    Launched from a shortcut there is no terminal to print to, so an
    unhandled error would otherwise be a window that never appears and no
    explanation anywhere.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("JARVIS could not start", message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(message)
    return 1


def main() -> int:
    missing = []
    for module, purpose in (
        ("customtkinter", "the window"),
        ("google.genai", "the agent"),
        ("dotenv", "reading .env"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(f"{module} ({purpose})")

    if missing:
        venv = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        return _fatal(
            "Missing packages:\n  "
            + "\n  ".join(missing)
            + "\n\n"
            + (
                f"You are running the wrong Python.\nUse:\n  {venv} app.py"
                if venv.exists()
                else "Run: pip install -r requirements.txt"
            )
        )

    try:
        from jarvis.app.tray import Tray
        from jarvis.app.window import JarvisWindow

        window = JarvisWindow()

        tray = Tray(window)
        if tray.start():
            window.attach_tray(tray)

        window.mainloop()
        return 0
    except Exception:  # noqa: BLE001
        return _fatal(traceback.format_exc()[-1800:])


if __name__ == "__main__":
    sys.exit(main())
