"""First-run setup.

Someone handed the executable has none of the context the author has. They do
not know what an API key is, where to get one, or that a free tier exists. If
the first thing they meet is a window saying "GEMINI_API_KEY is not set", they
close it and never open it again.

So this asks for exactly one thing, says precisely where to find it, opens the
page for them, and writes the .env itself. Everything else gets a sensible
default.

It used to call Google to confirm the key before accepting it. That read well
and behaved badly: on a machine behind a proxy, a captive portal, or simply
offline, the check became the only thing between the user and a working app,
and setup could not finish on exactly the machines that most needed a clear
explanation. It also required a worker thread, and that thread touched Tk
widgets from off the main thread, which throws where nobody can see it.

Now the key is checked for shape and saved. A key that is subtly wrong is
reported by the first message instead, which is a better place to learn it
than a setup screen that cannot complete.

  * **No network, no threads.** Setup finishes on any machine, instantly.
  * **The key is never echoed back or logged.** It goes from the field to the
    .env and nowhere else.
"""
from __future__ import annotations

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
    "Paste it in the box below and press Save.",
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


def key_format_problem(api_key: str) -> str:
    """Return a complaint about the key's shape, or "" if it looks usable.

    Shape only. Setup deliberately does not phone Google: on a blocked or
    offline network that check cannot succeed, and making it the gate meant
    setup could not finish on exactly the machines that most needed a clear
    explanation. Catching a paste that is obviously wrong is cheap and works
    everywhere; catching a key that is subtly wrong is the first message's
    job, where the error can say so directly.
    """
    key = api_key.strip()
    if not key:
        return "Paste your key first."
    if " " in key or "\n" in key:
        return "That has a space in it. Copy the key on its own."
    if len(key) < 20:
        return "That looks too short. Copy the whole key."
    if key.lower().startswith(("http://", "https://")):
        return "That is a web address, not a key. Copy the key from the page."
    return ""


class SetupWizard(ctk.CTkToplevel):
    """Modal first-run window. `completed` is True when setup succeeded."""

    def __init__(self, master=None) -> None:
        super().__init__(master)
        self.completed = False

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
            buttons, text="Save and start", height=44, fg_color=ACCENT,
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
        """Save the key and start. No network call.

        This used to phone Google to confirm the key before accepting it,
        which was a nice idea and a bad one in practice: on a network that
        blocks or silently drops the connection the check is the only thing
        standing between the user and a working app, and it turned setup into
        a wait with nothing to show for it.

        Worse, it needed a worker thread, and that thread called `self.after`
        on a widget the user might already have closed -- Tk is not
        thread-safe there, and it threw from inside the thread where nobody
        could see it.

        The key is checked for shape only and written. If it is wrong, the
        first message says so plainly, which is a better place to find out
        than a setup screen that cannot finish.
        """
        key = self.entry.get().strip()

        problem = key_format_problem(key)
        if problem:
            self._say(problem, ALARM)
            return

        try:
            path = write_env(key, self.user_entry.get(), self.name_entry.get())
        except OSError as exc:
            self._say(f"Could not save your settings: {exc}", ALARM)
            return

        self.completed = True
        self._say(f"Saved to {path}. Starting...", GOOD)
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
