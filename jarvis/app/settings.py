"""Choosing a provider and entering its key, at any time, from the app.

The setup wizard asks once, on first run, and only ever about Gemini. This is
the version you can open whenever you like: pick a different service, paste a
different key, and carry on in the same conversation.

**It takes effect immediately.** Saving rewrites .env, updates the running
process's environment, rebuilds the model ladder for the new provider and
drops the cached SDK client. Nothing restarts, because being told to restart
an app to change a setting is how a setting ends up changed once and never
again.

**Keys are never shown back.** An existing key appears as a masked hint of its
first and last few characters -- enough to recognise which key is in there,
useless to anyone reading over a shoulder or watching a screen share.
"""
from __future__ import annotations

import webbrowser

import customtkinter as ctk

from .. import providers

BG = "#0d1117"
PANEL = "#161b22"
FIELD = "#1c2128"
LINE = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
GOOD = "#3fb950"
ALARM = "#f85149"


def masked(key: str) -> str:
    """A key rendered so it can be recognised but not read."""
    if not key:
        return "not set"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]} ({len(key)} chars)"


class ProviderDialog(ctk.CTkToplevel):
    """Pick a provider, paste a key, save. Openable at any time."""

    def __init__(self, master, on_saved=None):
        super().__init__(master)
        self.on_saved = on_saved
        self._provider = providers.active()

        self.title("Model provider")
        self.configure(fg_color=BG)
        self.geometry("620x560")
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self, text="Model provider",
            font=ctk.CTkFont(size=18, weight="bold"), text_color=TEXT,
        ).pack(anchor="w", padx=22, pady=(20, 2))
        ctk.CTkLabel(
            self,
            text="Change this whenever you like. It applies straight away.",
            font=ctk.CTkFont(size=12), text_color=MUTED,
        ).pack(anchor="w", padx=22, pady=(0, 14))

        labels = [p.label for p in providers.PROVIDERS.values()]
        self._by_label = {p.label: p for p in providers.PROVIDERS.values()}
        self.choice = ctk.CTkOptionMenu(
            self, values=labels, command=self._changed,
            fg_color=FIELD, button_color=LINE, button_hover_color=ACCENT,
            text_color=TEXT, width=280,
        )
        self.choice.set(self._provider.label)
        self.choice.pack(anchor="w", padx=22)

        self.notes = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=560, justify="left",
        )
        self.notes.pack(anchor="w", padx=22, pady=(10, 0))

        self.current = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=560, justify="left",
        )
        self.current.pack(anchor="w", padx=22, pady=(6, 0))

        self.key_label = ctk.CTkLabel(
            self, text="API key", font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        )
        self.key_label.pack(anchor="w", padx=22, pady=(18, 4))

        self.entry = ctk.CTkEntry(
            self, width=560, height=38, show="*",
            fg_color=FIELD, border_color=LINE, text_color=TEXT,
            placeholder_text="paste your key here",
        )
        self.entry.pack(anchor="w", padx=22)
        self.entry.bind("<Return>", lambda _e: self._save())

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(anchor="w", padx=22, pady=(8, 0))
        self.show = ctk.CTkCheckBox(
            row, text="Show", command=self._toggle_show,
            text_color=MUTED, fg_color=ACCENT, hover_color=ACCENT,
            checkbox_width=18, checkbox_height=18,
        )
        self.show.pack(side="left")
        ctk.CTkButton(
            row, text="Where do I get one?", width=170, height=28,
            fg_color="transparent", hover_color=PANEL, text_color=ACCENT,
            command=self._open_console,
        ).pack(side="left", padx=(12, 0))

        self.message = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color=MUTED,
            wraplength=560, justify="left",
        )
        self.message.pack(anchor="w", padx=22, pady=(14, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", padx=22, pady=18)
        ctk.CTkButton(
            buttons, text="Cancel", width=110, height=38,
            fg_color="transparent", hover_color=PANEL,
            text_color=MUTED, border_width=1, border_color=LINE,
            command=self._close,
        ).pack(side="right")
        ctk.CTkButton(
            buttons, text="Save", width=140, height=38,
            fg_color=ACCENT, hover_color="#1f6feb", text_color="#04121f",
            command=self._save,
        ).pack(side="right", padx=(0, 10))

        self._changed(self._provider.label)
        self.after(120, self.entry.focus_set)

    # ------------------------------------------------------------ behaviour
    def _toggle_show(self) -> None:
        self.entry.configure(show="" if self.show.get() else "*")

    def _open_console(self) -> None:
        try:
            webbrowser.open(self._provider.console_url)
        except Exception:  # noqa: BLE001 - no browser is not fatal
            self.message.configure(
                text=f"Open this yourself: {self._provider.console_url}",
                text_color=MUTED,
            )

    def _changed(self, label: str) -> None:
        self._provider = self._by_label[label]
        provider = self._provider

        self.notes.configure(
            text=(provider.notes or "")
            + f"\nModels: {provider.fast} (fast), {provider.smart} (capable)"
        )
        existing = providers.key_for(provider)
        if not provider.needs_key:
            self.current.configure(
                text="No key needed -- this one runs on your machine.",
                text_color=GOOD,
            )
            self.entry.configure(state="disabled", placeholder_text="not needed")
            self.key_label.configure(text_color=MUTED)
        else:
            self.entry.configure(state="normal", placeholder_text=provider.key_hint)
            self.key_label.configure(text_color=TEXT)
            self.current.configure(
                text=f"Key on file: {masked(existing)}"
                + ("  -- leave blank to keep it" if existing else ""),
                text_color=GOOD if existing else MUTED,
            )
        self.message.configure(text="")

    def _save(self) -> None:
        typed = "" if self.entry.cget("state") == "disabled" else self.entry.get()
        problem = providers.save_choice(self._provider.key, typed.strip())
        if problem:
            self.message.configure(text=problem, text_color=ALARM)
            return

        # The cached SDK client belongs to the old provider and the old key.
        try:
            from ..client import client

            client.reset()
        except Exception:  # noqa: BLE001 - a failed reset must not trap anyone
            pass

        if self.on_saved is not None:
            self.on_saved(self._provider)
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()
