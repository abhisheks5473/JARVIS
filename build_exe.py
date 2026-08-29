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

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _output_root() -> Path:
    """Somewhere to build that OneDrive is not watching.

    This project lives under OneDrive, and OneDrive takes handles on files it
    is syncing. PyInstaller writes thousands of files and then deletes them,
    which collides with that constantly: two builds here died with
    "Access is denied: build\\JARVIS\\localpycs" partway through, and the
    second left a stale exe in dist that looked like a fresh success.

    Building into %LOCALAPPDATA% avoids the locking entirely, and avoids
    uploading several hundred megabytes of disposable build output to the
    user's cloud storage -- which is the more expensive mistake of the two.

    --here forces the old in-project behaviour.
    """
    if "--here" in sys.argv:
        return ROOT
    base = os.getenv("LOCALAPPDATA") or tempfile.gettempdir()
    out = Path(base) / "JARVIS-build"
    out.mkdir(parents=True, exist_ok=True)
    return out


OUT_ROOT = _output_root()
DIST = OUT_ROOT / "dist"
BUILD = OUT_ROOT / "build"

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


def _clear_workspace() -> None:
    """Remove build and dist, working around Windows file locking.

    A previous JARVIS.exe still running holds its own folder open, and
    OneDrive keeps handles on files it is syncing. Either makes PyInstaller
    die with "Access is denied" partway through -- and it did, leaving a stale
    exe in dist that looked like a fresh successful build. Stopping the
    process and retrying beats debugging that twice.
    """
    subprocess.run(
        ["taskkill", "/F", "/IM", "JARVIS.exe"],
        capture_output=True,
        check=False,
    )

    for folder in (BUILD, DIST):
        for attempt in range(4):
            if not folder.exists():
                break
            try:
                shutil.rmtree(folder)
                break
            except (OSError, PermissionError):
                if attempt == 3:
                    print(
                        f"warning: could not fully remove {folder}. If the build "
                        "fails, close anything using it and try again."
                    )
                time.sleep(1.5)


def build(lite: bool = False, onefile: bool = False) -> int:
    icon = ROOT / "jarvis.ico"
    name = "JARVIS"

    _clear_workspace()

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
    folder_note = (
        ""
        if onefile
        else (
            "EXTRACT THE WHOLE FOLDER FIRST\n"
            "------------------------------\n"
            "Do not drag JARVIS.exe out of the zip on its own. It needs the\n"
            "_internal folder sitting next to it. Right-click the zip, choose\n"
            "Extract All, and run JARVIS.exe from the extracted folder.\n\n"
            "Running it from inside the zip viewer does not work either:\n"
            "Windows only unpacks the one file you double-clicked.\n\n"
        )
    )

    readme.write_text(
        "JARVIS\n"
        "======\n\n"
        + folder_note
        + "Double-click JARVIS.exe.\n\n"
        "The first time it runs it asks for a Google API key and shows you\n"
        "exactly where to get one. It is free, takes about a minute, and does\n"
        "not need a credit card.\n\n"
        "IF WINDOWS BLOCKS IT\n"
        "--------------------\n"
        "SmartScreen will say the publisher is unknown, because this is not\n"
        "code-signed. Click More info, then Run anyway.\n\n"
        "Antivirus sometimes deletes parts of apps built this way. It is a\n"
        "false positive, but the result is a half-removed app that starts and\n"
        "then complains a component is missing. If that happens, restore the\n"
        "files from quarantine or add this folder to the exclusions, then\n"
        "extract again.\n\n"
        "IF IT WILL NOT START\n"
        "--------------------\n"
        "Open a Command Prompt in this folder and run:\n"
        "  JARVIS.exe --selftest\n"
        "That writes selftest.txt listing every component and whether it\n"
        "loaded. Send that file to whoever gave you this.\n\n"
        "PRIVACY\n"
        "-------\n"
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
