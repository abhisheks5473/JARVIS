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

    if getattr(sys, "frozen", False):
        return None  # a bundled build has no venv to relaunch into
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


def _selftest() -> int:
    """Import everything and write a report next to the executable.

    A windowed build has no console, so a bundling mistake is invisible until
    a recipient presses a button that quietly does nothing. This forces every
    optional subsystem to import and records what happened, which is also the
    fastest way to diagnose someone else's machine: "run JARVIS.exe
    --selftest and send me the file".
    """
    import platform
    from datetime import datetime

    # Required: without these there is no application.
    required = [
        ("customtkinter", "the window"),
        ("pystray", "tray icon"),
        ("PIL", "icons and screenshot scaling"),
        ("google.genai", "the agent"),
        ("httpx", "web"),
        ("bs4", "reading pages"),
        ("psutil", "system stats"),
        ("pyautogui", "mouse and keyboard"),
        ("pygetwindow", "window control"),
        ("pyperclip", "clipboard"),
        ("pynput", "hotkeys"),
        ("mss", "screen capture"),
        ("win32api", "media keys"),
        ("apscheduler", "scheduled jobs"),
        ("sqlite3", "memory and quota"),
    ]

    # Optional: the lite build leaves these out on purpose. Reporting their
    # absence as a failure told the reader the app was damaged when it was
    # merely smaller, which is the opposite of what a diagnostic is for.
    optional = [
        ("faster_whisper", "speech to text"),
        ("ctranslate2", "speech to text engine"),
        ("sounddevice", "microphone and speaker"),
        ("piper", "text to speech"),
        ("onnxruntime", "text to speech engine"),
        ("av", "audio decoding"),
        ("googleapiclient", "calendar and mail"),
    ]

    lines = [
        "JARVIS self-test",
        datetime.now().astimezone().isoformat(timespec="seconds"),
        f"{platform.system()} {platform.release()}  python {sys.version.split()[0]}",
        f"frozen: {getattr(sys, 'frozen', False)}",
        "",
    ]
    failed = 0
    for module, purpose in required:
        try:
            __import__(module)
            lines.append(f"  ok    {module:18} {purpose}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            lines.append(f"  FAIL  {module:18} {purpose} -- {type(exc).__name__}: {exc}")

    absent = 0
    for module, purpose in optional:
        try:
            __import__(module)
            lines.append(f"  ok    {module:18} {purpose}")
        except Exception:  # noqa: BLE001
            absent += 1
            lines.append(f"  --    {module:18} {purpose} (not in this build)")

    try:
        from jarvis import config
        from jarvis.tools import registry

        lines += [
            "",
            f"  data directory: {config.ROOT}",
            f"  tools registered: {len(registry)}",
            f"  api key present: {config.api_key_present()}",
        ]
    except Exception as exc:  # noqa: BLE001
        failed += 1
        lines.append(f"  FAIL  jarvis core -- {type(exc).__name__}: {exc}")

    if failed:
        verdict = (
            f"{failed} required component(s) missing -- this copy is damaged. "
            "Extract the whole folder again, or check antivirus quarantine."
        )
    elif absent:
        verdict = (
            f"Working. {absent} optional component(s) not included in this "
            "build (voice and calendar are left out of the lite build)."
        )
    else:
        verdict = "Working. All subsystems available."
    lines += ["", verdict]
    report = "\n".join(lines)

    for target in (Path(sys.executable).parent / "selftest.txt", ROOT / "selftest.txt"):
        try:
            target.write_text(report, encoding="utf-8")
            break
        except OSError:
            continue

    print(report)
    return 1 if failed else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

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
        # A frozen build has no virtual environment and never will. Telling
        # its recipient to run `python -m venv` is advice they cannot act on
        # and did not sign up for -- they were handed an application. If
        # imports are failing here the bundle itself is damaged, which is
        # exactly what happens when a folder is part-copied, part-extracted,
        # or half-removed by a sync client.
        if getattr(sys, "frozen", False):
            return _fatal(
                "This copy of JARVIS is incomplete:\n  "
                + "\n  ".join(missing)
                + "\n\nSome of its files are missing.\n\n"
                "Most likely the folder was only partly copied, or the zip was\n"
                "not fully extracted. Copy or extract the whole JARVIS folder\n"
                "again, keeping JARVIS.exe and the _internal folder together,\n"
                "then run it from there.\n\n"
                "Running JARVIS.exe --selftest writes selftest.txt listing\n"
                "exactly what is missing."
            )

        # Running from source, the usual cause is the wrong interpreter, and
        # that is fixable without bothering anyone about it.
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
        from jarvis.app.setup_wizard import needs_setup, run_setup

        # First run on someone else's machine: they have no key and no idea
        # they need one. Ask before building a window that cannot think.
        if needs_setup():
            if not run_setup():
                return 0  # they quit the wizard; not an error
            # config read .env at import time, so re-read it now that one
            # exists, rather than starting a keyless agent.
            import importlib

            from dotenv import load_dotenv

            from jarvis import config as _config

            load_dotenv(_config.ENV_FILE, override=True)
            importlib.reload(_config)

        from jarvis.app.single import claim
        from jarvis.app.tray import Tray
        from jarvis.app.window import JarvisWindow

        # One copy at a time. A second launch hands the request to the copy
        # already running and stops here, rather than starting a rival with
        # its own wake-word listener on the same microphone.
        pending_show = []
        lock = claim(on_show=lambda: pending_show.append(True))
        if lock is None:
            return 0

        window = JarvisWindow()
        window.single_lock = lock

        # The socket thread cannot touch Tk, so it leaves a note and the UI
        # thread picks it up.
        def _watch_for_second_launch() -> None:
            if pending_show:
                pending_show.clear()
                try:
                    window.show_window()
                except Exception:  # noqa: BLE001
                    pass
            window.after(400, _watch_for_second_launch)

        window.after(400, _watch_for_second_launch)

        tray = Tray(window)
        if tray.start():
            window.attach_tray(tray)

        window.mainloop()
        return 0
    except Exception:  # noqa: BLE001
        return _fatal(traceback.format_exc()[-1800:])


if __name__ == "__main__":
    sys.exit(main())
