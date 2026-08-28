"""Put JARVIS in the Start menu, and optionally start it at login.

    python make_shortcut.py             Start menu entry
    python make_shortcut.py --startup   ...and launch at login

The shortcut points at JARVIS.vbs, which runs pythonw, so there is no console
window at any point.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "JARVIS.vbs"


def make(folder: Path, name: str = "JARVIS.lnk") -> Path:
    try:
        from win32com.client import Dispatch
    except ImportError:
        raise SystemExit("pywin32 is required: pip install pywin32") from None

    folder.mkdir(parents=True, exist_ok=True)
    link = folder / name
    shortcut = Dispatch("WScript.Shell").CreateShortCut(str(link))
    shortcut.Targetpath = str(TARGET)
    shortcut.WorkingDirectory = str(ROOT)
    shortcut.Description = "JARVIS assistant"
    shortcut.save()
    return link


def main() -> int:
    if not TARGET.exists():
        raise SystemExit(f"missing {TARGET}")

    appdata = Path(os.environ["APPDATA"])
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"

    made = [make(programs)]
    if "--startup" in sys.argv:
        made.append(make(programs / "Startup"))

    for link in made:
        print("created:", link)
    print()
    print("Search the Start menu for JARVIS, or double-click JARVIS.vbs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
