"""First-run setup.

Someone handed the executable has none of the context the author has. They do
not know what an API key is, where to get one, or that a free tier exists. If
the first thing they meet is a window saying "GEMINI_API_KEY is not set", they
close it and never open it again.

So this asks for exactly one thing, says precisely where to find it, opens the
page for them, checks the key actually works before accepting it, and writes
the .env itself. Everything else gets a sensible default.

Three details that matter more than they look:

  * **The key is verified, not just stored.** A typo saved silently becomes a
    confusing failure ten minutes later in a different part of the program.
  * **Verification runs off the UI thread.** A network call on the main thread
    freezes the window, which reads as a crash.
  * **The key is never echoed back or logged.** It goes from the field to the
    .env and nowhere else.
"""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import customtkinter as ctk

from .. import config

BG = "#0d1117"
PANEL = "#161b22"
FIELD = "#1c2128"
LINE = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
GOOD = "#3fb950"
ALARM = "#f85149"

KEY_URL = "https://aistudio.google.com/apikey"

STEPS = [
    "Click the button below. It opens Google AI Studio in your browser.",
    "Sign in with any Google account. A personal one is fine.",
    'Click "Create API key", then "Create API key in new project".',
    "Copy the key it shows you.",
    "Paste it in the box below and press Verify.",
]


def needs_setup() -> bool:
    """True when there is no usable key anywhere."""
    return not config.api_key_present()


def write_env(api_key: str, user_name: str = "", assistant_name: str = "") -> Path:
    """Write or update the .env, preserving anything already in it."""
    target = config.ENV_FILE
    existing: dict[str, str] = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    existing["GEMINI_API_KEY"] = api_key.strip()
    if user_name.strip():
        existing["JARVIS_USER_NAME"] = user_name.strip()
    if assistant_name.strip():
        existing["JARVIS_NAME"] = assistant_name.strip()
    existing.setdefault("JARVIS_APPROVAL", "never")
    existing.setdefault("JARVIS_TAINT_GUARD", "1")
    existing.setdefault("JARVIS_STORE", "0")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Written by the JARVIS setup wizard. Keep this file private.\n"
        + "\n".join(f"{k}={v}" for k, v in existing.items())
        + "\n",
        encoding="utf-8",
    )
    return target


# Long enough for a slow connection, short enough that a blocked one gets
# reported rather than waited on. Without this the SDK waits indefinitely and
# the wizard sat on "Checking..." forever, with no way out.
VERIFY_TIMEOUT_S = 25


def verify_key(api_key: str) -> tuple[bool, str]:
    """Ask Google whether this key is real.

    Listing models authenticates without spending generation quota, so a
    mistyped key costs the user nothing to discover.

    Two things here are load-bearing:

    * **An explicit timeout.** The default is none at all, so on a network
      that silently drops the connection -- a captive portal, a corporate
      proxy, a firewall -- this blocked forever.
    * **Only the first model is read.** `models.list()` returns a pager, and
      draining it fetches every page. One model is all the proof needed that
      the key authenticates.
    """
    key = api_key.strip()
    if not key:
        return False, "Paste your key first."
    if len(key) < 20 or " " in key:
        return False, "That does not look like a key. Copy the whole thing."

    try:
        import os

        from google import genai

        os.environ["GEMINI_API_KEY"] = key
        client = genai.Client(
            api_key=key,
            http_options={"timeout": VERIFY_TIMEOUT_S * 1000},  # milliseconds
        )
        names = []
        for model in client.models.list():
            names.append(model.name)
            break  # one is enough; do not walk every page
    except Exception as exc:  # noqa: BLE001 - any failure is a failed key
        detail = str(exc)
        if "API_KEY_INVALID" in detail or "API key not valid" in detail:
            return False, "Google rejected that key. Check you copied all of it."
        if "PERMISSION_DENIED" in detail or "403" in detail:
            return False, "That key exists but is not permitted to use the API."
        if any(
            word in detail.lower()
            for word in ("timeout", "timed out", "connect", "ssl", "resolve", "network")
        ):
            return False, (
                "Could not reach Google. This computer may be offline, behind a "
                "proxy, or on a network that blocks it. Check the connection and "
                "try again."
            )
        return (
            False,
            f"Could not reach Google ({type(exc).__name__}). Check your connection.",
        )

    if not names:
        return False, "The key works but no models are visible to it."
    # Deliberately no count: only the first page's first model was read, so
    # any number here would be a number this function did not measure.
    return True, "Verified."


class SetupWizard(ctk.CTkToplevel):
    """Modal first-run window. `completed` is True when setup succeeded."""

    def __init__(self, master=None) -> None:
        super().__init__(master)
        self.completed = False
        self._attempt = 0

        self.title("Welcome to JARVIS")
        self.configure(fg_color=BG)
        self.geometry("640x700")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._build()
        self.after(120, self.lift)
        self.after(150, lambda: self.entry.focus_set())

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        ctk.CTkLabel(
            self, text="JARVIS", font=ctk.CTkFont(size=30, weight="bold"),
            text_color=ACCENT,
        ).pack(pady=(28, 2))
        ctk.CTkLabel(
            self,
            text="A voice assistant that runs on this computer.\n"
                 "It needs one free key from Google before it can think.",
            text_color=MUTED, justify="center",
        ).pack(pady=(0, 18))

        card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        card.pack(fill="x", padx=28)

        ctk.CTkLabel(
            card, text="Getting your key takes about a minute and costs nothing.",
            text_color=TEXT, font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(16, 10))

        for number, step in enumerate(STEPS, 1):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=2)
            ctk.CTkLabel(
                row, text=str(number), width=22, text_color=ACCENT,
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(side="left", anchor="n")
            ctk.CTkLabel(
                row, text=step, text_color=MUTED, justify="left", wraplength=500,
                font=ctk.CTkFont(size=12),
            ).pack(side="left", anchor="w")

        ctk.CTkButton(
            card, text="Open Google AI Studio", height=38, fg_color=ACCENT,
            hover_color="#1f6feb", command=self._open_browser,
        ).pack(fill="x", padx=20, pady=(14, 6))
        ctk.CTkLabel(
            card, text=KEY_URL, text_color=MUTED, font=ctk.CTkFont(size=10)
        ).pack(pady=(0, 16))

        ctk.CTkLabel(
            self, text="Paste your key here", text_color=TEXT,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=30, pady=(18, 6))

        # show="*" so a shoulder-surfer or a screenshot does not capture it.
        self.entry = ctk.CTkEntry(
            self, height=42, show="*", fg_color=FIELD, border_color=LINE,
            text_color=TEXT, placeholder_text="paste the key",
        )
        self.entry.pack(fill="x", padx=30)
        self.entry.bind("<Return>", lambda _e: self._verify())

        names = ctk.CTkFrame(self, fg_color="transparent")
        names.pack(fill="x", padx=30, pady=(12, 0))
        self.user_entry = ctk.CTkEntry(
            names, height=36, fg_color=FIELD, border_color=LINE, text_color=TEXT,
            placeholder_text="What should it call you? (optional)",
        )
        self.user_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.name_entry = ctk.CTkEntry(
            names, height=36, width=170, fg_color=FIELD, border_color=LINE,
            text_color=TEXT, placeholder_text="Its name (JARVIS)",
        )
        self.name_entry.pack(side="left")

        self.message = ctk.CTkLabel(
            self, text="", text_color=MUTED, wraplength=560, justify="left"
        )
        self.message.pack(fill="x", padx=30, pady=(14, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=30, pady=(16, 22), side="bottom")
        self.verify_button = ctk.CTkButton(
            buttons, text="Verify and start", height=44, fg_color=ACCENT,
            hover_color="#1f6feb", command=self._verify,
        )
        self.verify_button.pack(side="right")
        ctk.CTkButton(
            buttons, text="Quit", width=100, height=44, fg_color=FIELD,
            hover_color=LINE, text_color=MUTED, command=self._cancel,
        ).pack(side="right", padx=(0, 10))

    # ------------------------------------------------------------ actions
    def _open_browser(self) -> None:
        try:
            webbrowser.open(KEY_URL)
            self._say("Opened in your browser. Copy the key, then come back.", MUTED)
        except Exception:  # noqa: BLE001
            self._say(f"Could not open a browser. Go to {KEY_URL}", ALARM)

    def _say(self, text: str, colour: str = MUTED) -> None:
        self.message.configure(text=text, text_color=colour)

    def _verify(self) -> None:
        key = self.entry.get().strip()
        if not key:
            self._say("Paste your key first.", ALARM)
            return

        self.verify_button.configure(state="disabled", text="Checking...")
        self._say("Asking Google whether that key works...", MUTED)
        self._attempt += 1

        # Off the UI thread: a network call here would freeze the window and
        # look like a crash.
        threading.Thread(target=self._verify_worker, args=(key,), daemon=True).start()

        # A belt-and-braces watchdog. The timeout inside verify_key should
        # always fire first, but a hung socket that ignores it would otherwise
        # leave this window on "Checking..." indefinitely -- which is exactly
        # what happened, and a stuck button with no explanation is the worst
        # possible first impression.
        self.after(
            (VERIFY_TIMEOUT_S + 8) * 1000,
            lambda a=self._attempt: self._verify_stalled(a),
        )

    def _verify_stalled(self, attempt: int) -> None:
        if attempt != self._attempt or self.completed:
            return  # a result already arrived, or a newer attempt superseded this
        self.verify_button.configure(state="normal", text="Verify and start")
        self._say(
            "No answer from Google after "
            f"{VERIFY_TIMEOUT_S + 8} seconds. This computer may be offline, "
            "behind a proxy, or on a network that blocks Google. You can try "
            "again, or quit and set the key up later.",
            ALARM,
        )

    def _verify_worker(self, key: str) -> None:
        ok, message = verify_key(key)
        self.after(0, lambda: self._verify_done(key, ok, message))

    def _verify_done(self, key: str, ok: bool, message: str) -> None:
        self._attempt += 1  # any pending watchdog for the old attempt goes quiet
        self.verify_button.configure(state="normal", text="Verify and start")
        if not ok:
            self._say(message, ALARM)
            return

        try:
            path = write_env(key, self.user_entry.get(), self.name_entry.get())
        except OSError as exc:
            self._say(
                f"Key is good, but the settings file could not be saved: {exc}", ALARM
            )
            return

        self.completed = True
        self._say(f"{message} Saved to {path}. Starting...", GOOD)
        self.after(900, self._finish)

    def _finish(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    def _cancel(self) -> None:
        self.completed = False
        self._finish()


def run_setup() -> bool:
    """Show the wizard standalone. True if the user completed it."""
    root = ctk.CTk()
    root.withdraw()
    wizard = SetupWizard(root)
    wizard.grab_set()
    root.wait_window(wizard)
    completed = wizard.completed
    root.destroy()
    return completed
