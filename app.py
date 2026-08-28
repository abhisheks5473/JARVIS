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


def _relaunch_under_venv() -> int | None:
    """Start again under the project's own interpreter, then step aside.

    The dependencies live in .venv, so any other Python fails on import. Rather
    than explaining that in a dialog and making it the reader's problem, this
    re-runs itself with the right interpreter and exits. It means `python
    app.py`, a double-click, and a shortcut all work the same way without
    anyone having to activate anything.

    pythonw is used so no console window appears. The guard variable stops an
    infinite relaunch if the venv is itself broken.
    """
    import os
    import subprocess

    if os.environ.get("JARVIS_RELAUNCHED") == "1":
        return None  # already tried; fall through to the error dialog

    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists() or Path(sys.executable).parent == pythonw.parent:
        return None

    env = dict(os.environ, JARVIS_RELAUNCHED="1")
    try:
        subprocess.Popen(
            [str(pythonw), str(ROOT / "app.py")],
            cwd=str(ROOT),
            env=env,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    return 0


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
        # Wrong interpreter is the usual cause, and it is fixable without
        # bothering anyone about it.
        relaunched = _relaunch_under_venv()
        if relaunched is not None:
            return relaunched

        venv = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        return _fatal(
            "Missing packages:\n  "
            + "\n  ".join(missing)
            + "\n\n"
            + (
                "The virtual environment exists but could not be used.\n"
                f"Try running:\n  {venv} app.py"
                if venv.exists()
                else "Set up the environment first:\n"
                "  python -m venv .venv\n"
                "  .venv\\Scripts\\pip install -r requirements.txt"
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
