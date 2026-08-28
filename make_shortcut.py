"""Make JARVIS double-clickable.

    python make_shortcut.py             Desktop + Start menu
    python make_shortcut.py --startup   ...and launch at login

Why this exists: a .py file is not double-clickable on a default Windows
install. There is usually no association for it at all, so double-clicking
one opens the "how do you want to open this file?" dialog and nothing runs.
Even where an association exists it points at the *system* Python, which does
not have this project's dependencies.

So the thing you double-click is a shortcut to JARVIS.vbs, which runs the
venv's pythonw -- no console window, correct interpreter, every time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "JARVIS.vbs"
ICON = ROOT / "jarvis.ico"


def ensure_icon() -> Path | None:
    """Draw the icon if it is missing, so a fresh clone still looks right."""
    if ICON.exists():
        return ICON
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = size // 32
    draw.ellipse(
        (pad, pad, size - pad - 1, size - pad - 1),
        fill=(13, 17, 23, 255),
        outline=(88, 166, 255, 255),
        width=size // 16,
    )
    inner = size // 3
    draw.ellipse(
        (inner, inner, size - inner - 1, size - inner - 1),
        fill=(88, 166, 255, 255),
    )
    image.save(
        ICON,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return ICON


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
    if ICON.exists():
        shortcut.IconLocation = str(ICON)
    shortcut.save()
    return link


def main() -> int:
    if not TARGET.exists():
        raise SystemExit(f"missing {TARGET}")
    ensure_icon()

    try:
        from win32com.client import Dispatch
    except ImportError:
        raise SystemExit("pywin32 is required: pip install pywin32") from None

    # Ask the shell where the Desktop is rather than assuming ~/Desktop.
    # OneDrive redirects it on most machines, and guessing lands the shortcut
    # somewhere the user will never see it.
    shell = Dispatch("WScript.Shell")
    desktop = Path(shell.SpecialFolders("Desktop"))
    programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"

    made = [make(desktop), make(programs)]
    if "--startup" in sys.argv:
        made.append(make(programs / "Startup"))

    for link in made:
        print("created:", link)
    print()
    print("Double-click JARVIS on your Desktop, or search the Start menu for it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
