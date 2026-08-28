"""Build a JARVIS.exe you can hand to someone else.

    python build_exe.py            full build, voice included
    python build_exe.py --lite     no voice, no Google Workspace (much smaller)
    python build_exe.py --onefile  a single .exe instead of a folder

The recipient needs nothing installed -- not Python, not pip, not a virtual
environment. Everything is bundled at build time, which is why "install what
is needed" happens here rather than on their machine: asking a stranger to run
pip is asking them to give up.

On first launch they get the setup wizard, which explains where to find a free
Google API key, opens the page, verifies what they paste, and writes the
settings file. After that it starts straight into the app.

Notes from making this actually work:

  * **onedir beats onefile.** A one-file build unpacks the whole bundle to a
    temp folder on every launch, which takes many seconds and looks broken.
    The folder build starts quickly. --onefile exists because it is easier to
    send.
  * **The ML wheels need help.** ctranslate2, onnxruntime and av ship native
    libraries PyInstaller does not find by following imports, so they are
    collected explicitly.
  * **--lite is worth offering.** googleapiclient alone is 100MB and only
    powers calendar and mail; the voice stack is another 210MB. Dropping both
    turns a large download into something you can actually send.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# Imported lazily or by name at runtime, so PyInstaller cannot see them.
HIDDEN = [
    "customtkinter", "pystray", "PIL", "PIL.Image", "PIL.ImageDraw",
    "google.genai", "dotenv", "httpx", "bs4", "psutil", "pyperclip",
    "pygetwindow", "pyautogui", "pynput", "pynput.keyboard",
    "pynput.keyboard._win32", "pynput.mouse", "pynput.mouse._win32",
    "apscheduler", "apscheduler.schedulers.background",
    "apscheduler.triggers.cron", "apscheduler.triggers.interval",
    "win32api", "win32con", "win32com.client", "mss", "mss.windows",
    "sqlite3", "zoneinfo", "tzdata",
]

VOICE_HIDDEN = [
    "faster_whisper", "ctranslate2", "sounddevice", "piper", "onnxruntime", "av",
]
GOOGLE_HIDDEN = [
    "googleapiclient", "googleapiclient.discovery", "google_auth_oauthlib",
]

# Packages whose data files and native libraries must be copied wholesale.
COLLECT = ["customtkinter", "pystray"]
VOICE_COLLECT = ["ctranslate2", "onnxruntime", "piper", "av", "faster_whisper"]

EXCLUDE = [
    "matplotlib", "scipy", "pandas", "IPython", "jupyter", "notebook",
    "pytest", "PyInstaller", "setuptools", "pip",
]


def build(lite: bool = False, onefile: bool = False) -> int:
    icon = ROOT / "jarvis.ico"
    name = "JARVIS"

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",                       # no console window
        "--name", name,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
    ]
    args += ["--onefile"] if onefile else ["--onedir"]
    if icon.exists():
        args += ["--icon", str(icon)]

    hidden = list(HIDDEN)
    collect = list(COLLECT)
    excluded = list(EXCLUDE)

    if lite:
        # Excluded rather than merely not-imported: PyInstaller would find
        # them through the tool modules and bundle them anyway.
        excluded += VOICE_HIDDEN + GOOGLE_HIDDEN
    else:
        hidden += VOICE_HIDDEN + GOOGLE_HIDDEN
        collect += VOICE_COLLECT

    for module in hidden:
        args += ["--hidden-import", module]
    for package in collect:
        args += ["--collect-all", package]
    for module in excluded:
        args += ["--exclude-module", module]

    args.append(str(ROOT / "app.py"))

    print("Building" + (" (lite)" if lite else "") + ". This takes a few minutes.\n")
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode != 0:
        print("\nBuild failed.")
        return result.returncode

    target = DIST / (f"{name}.exe" if onefile else name)
    if not target.exists():
        print(f"\nBuild reported success but {target} is missing.")
        return 1

    size = (
        target.stat().st_size
        if onefile
        else sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    )

    # A README beside the exe, because the person receiving it did not read
    # anything else.
    readme = (DIST if onefile else target) / "READ ME FIRST.txt"
    readme.write_text(
        "JARVIS\n"
        "======\n\n"
        "Double-click JARVIS.exe.\n\n"
        "The first time it runs it asks for a Google API key and shows you\n"
        "exactly where to get one. It is free, takes about a minute, and does\n"
        "not need a credit card.\n\n"
        "Windows may warn that the publisher is unknown, because this is not\n"
        "code-signed. Click More info, then Run anyway.\n\n"
        "Your key and conversation history stay on this computer, under:\n"
        "  %LOCALAPPDATA%\\JARVIS\n\n"
        "To remove it: delete this folder and that one.\n",
        encoding="utf-8",
    )

    print("\n" + "=" * 62)
    print(f"Built: {target}")
    print(f"Size:  {size / 1024 / 1024:,.0f} MB")
    if not onefile:
        print(f"\nShare the whole '{name}' folder (zip it first).")
    print("Recipients need nothing installed. First run asks for their key.")
    print("=" * 62)
    return 0


def main() -> int:
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is missing. Run: pip install pyinstaller")
            return 1

    return build(lite="--lite" in sys.argv, onefile="--onefile" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
