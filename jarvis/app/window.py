"""The application window.

The terminal HUD existed because a non-deterministic system you cannot see
inside is one you debug by superstition. That argument does not weaken in a
GUI, so the same information is here: which tools fired and how long they
took, which model answered, the quota burn-down, and the security state of the
conversation.

Three rules hold the design together:

  * **Nothing blocks the main thread.** Every agent turn happens on a worker;
    this file drains an event queue on a timer and paints.
  * **The conversation is not the only thing worth showing.** Tool activity
    appears inline as it happens, so a slow turn is legibly slow rather than
    apparently frozen.
  * **The security banner is not decoration.** When injected content is
    detected it takes over the top of the window, because with routine
    approval prompts off it is the one thing you need to notice.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import font as tkfont

import customtkinter as ctk

from .. import config
from ..quota import Mode, governor
from .bridge import AgentBridge, Event

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# A restrained palette: one accent, a warning, an alarm, and greys. More
# colours than that and the status line stops meaning anything.
BG = "#0d1117"
PANEL = "#161b22"
FIELD = "#1c2128"
LINE = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
GOOD = "#3fb950"
WARN = "#d29922"
ALARM = "#f85149"

_MODE_COLOUR = {
    Mode.NORMAL: GOOD,
    Mode.CONSERVE: WARN,
    Mode.CRITICAL: "#db6d28",
    Mode.EXHAUSTED: ALARM,
}


class ApprovalDialog(ctk.CTkToplevel):
    """The one prompt left, so it is worth showing properly.

    Routine approval is off. This appears only when the taint guard fires --
    injected content has been read and a destructive action followed it --
    which is exactly when a dialog should be hard to dismiss by reflex.
    """

    def __init__(self, master, tool: str, arguments: dict, reason: str, on_answer):
        super().__init__(master)
        self.on_answer = on_answer
        self.answered = False

        self.title("Approval required")
        self.configure(fg_color=BG)
        self.geometry("560x420")
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False))

        hostile = "INJECTION RISK" in reason
        ctk.CTkLabel(
            self,
            text="INJECTION RISK" if hostile else "Confirm action",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=ALARM if hostile else WARN,
        ).pack(anchor="w", padx=20, pady=(18, 4))

        ctk.CTkLabel(
            self, text=reason, wraplength=510, justify="left", text_color=TEXT
        ).pack(anchor="w", padx=20, pady=(0, 12))

        box = ctk.CTkTextbox(
            self, height=190, fg_color=FIELD, text_color=TEXT, border_color=LINE,
            border_width=1, font=ctk.CTkFont(family="Consolas", size=12),
        )
        box.pack(fill="both", expand=True, padx=20)
        box.insert(
            "1.0",
            f"{tool}\n"
            + "\n".join(f"  {k} = {str(v)[:600]}" for k, v in arguments.items()),
        )
        box.configure(state="disabled")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(
            row, text="Deny", width=120, fg_color=FIELD, hover_color=LINE,
            text_color=TEXT, command=lambda: self._answer(False),
        ).pack(side="right")
        ctk.CTkButton(
            row, text="Allow once", width=140,
            fg_color=ALARM if hostile else ACCENT,
            hover_color="#c23c33" if hostile else "#1f6feb",
            command=lambda: self._answer(True),
        ).pack(side="right", padx=(0, 10))

        # Escape refuses, so the reflexive keystroke is the safe one.
        self.bind("<Escape>", lambda _e: self._answer(False))
        self.after(80, self.lift)

    def _answer(self, approved: bool) -> None:
        if self.answered:
            return
        self.answered = True
        self.on_answer(approved)
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


class JarvisWindow(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.bridge = AgentBridge()
        self.hotkeys = None
        self.nerves = None
        self._tray = None
        self._listening = False

        self.title(config.INTEGRATIONS.assistant_name)
        self.geometry("980x760")
        self.minsize(720, 520)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._build()
        self.after(60, self._boot)
        self.after(80, self._pump)
        self.after(1000, self._tick_status)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=54)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_propagate(False)

        ctk.CTkLabel(
            header, text=config.INTEGRATIONS.assistant_name,
            font=ctk.CTkFont(size=18, weight="bold"), text_color=ACCENT,
        ).pack(side="left", padx=(18, 14))

        self.model_label = ctk.CTkLabel(
            header, text="", text_color=MUTED, font=ctk.CTkFont(size=11)
        )
        self.model_label.pack(side="left")

        self.security_label = ctk.CTkLabel(
            header, text="secure", text_color=GOOD,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.security_label.pack(side="right", padx=(0, 18))

        self.quota_label = ctk.CTkLabel(
            header, text="", text_color=MUTED, font=ctk.CTkFont(size=11)
        )
        self.quota_label.pack(side="right", padx=(0, 16))

        self.banner = ctk.CTkFrame(self, fg_color="#3d1518", corner_radius=0)
        self.banner_label = ctk.CTkLabel(
            self.banner, text="", text_color=ALARM, wraplength=900,
            justify="left", font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.banner_label.pack(anchor="w", padx=18, pady=8)

        self.view = tk.Text(
            self, wrap="word", bg=BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", padx=20, pady=16, spacing1=2, spacing3=6,
            font=("Segoe UI", 11), state="disabled", highlightthickness=0,
        )
        self.view.grid(row=2, column=0, sticky="nsew")

        scroll = ctk.CTkScrollbar(self, command=self.view.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.view.configure(yscrollcommand=scroll.set)

        base = tkfont.Font(font=("Segoe UI", 11))
        bold = base.copy()
        bold.configure(weight="bold")
        mono = tkfont.Font(family="Consolas", size=10)

        self.view.tag_configure("you", foreground=ACCENT, font=bold, spacing1=10)
        self.view.tag_configure("jarvis", foreground=TEXT, spacing3=10)
        self.view.tag_configure("tool", foreground=MUTED, font=mono)
        self.view.tag_configure("ok", foreground=GOOD, font=mono)
        self.view.tag_configure("bad", foreground=ALARM, font=mono)
        self.view.tag_configure("warn", foreground=WARN, font=mono)
        self.view.tag_configure("muted", foreground=MUTED, font=mono)

        bar = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(
            bar, placeholder_text="Ask me something...", height=44,
            fg_color=FIELD, border_color=LINE, text_color=TEXT,
            font=ctk.CTkFont(size=13),
        )
        self.entry.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=12)
        self.entry.bind("<Return>", lambda _e: self._submit())

        self.mic_button = ctk.CTkButton(
            bar, text="Talk", width=76, height=44, fg_color=FIELD,
            hover_color=LINE, text_color=TEXT, command=self.bridge.listen,
        )
        self.mic_button.grid(row=0, column=1, pady=12)

        self.send_button = ctk.CTkButton(
            bar, text="Send", width=76, height=44, fg_color=ACCENT,
            hover_color="#1f6feb", command=self._submit,
        )
        self.send_button.grid(row=0, column=2, padx=(8, 16), pady=12)

        self.status = ctk.CTkLabel(
            self, text="starting...", text_color=MUTED, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self.status.grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 8)
        )

    # ------------------------------------------------------------ writing
    def _write(self, text: str, tag: str = "jarvis") -> None:
        self.view.configure(state="normal")
        self.view.insert("end", text + "\n", tag)
        self.view.configure(state="disabled")
        self.view.see("end")

    # ------------------------------------------------------------ boot
    def _boot(self) -> None:
        self._write("Starting up.", "muted")
        self.bridge.start()

        if not config.api_key_present():
            self._write(
                "No GEMINI_API_KEY in .env, so nothing will reach the model. "
                "Get one free at aistudio.google.com/apikey.", "bad",
            )

        self._start_hotkeys()
        self._start_scheduler()
        self._start_wakeword()

        if self._tray is None:
            # Without a tray there is nowhere to hide to, and closing the
            # window quits. Worth saying once, rather than being discovered.
            self._write("No tray icon here, so closing this window quits.", "muted")

    def _start_hotkeys(self) -> None:
        try:
            from ..voice.hotkey import default_hotkeys

            self.hotkeys = default_hotkeys(
                on_talk=lambda: self.after(0, self.bridge.listen),
                on_kill=lambda: self.after(0, self.quit_app),
                on_interrupt=lambda: self.after(0, self.bridge.stop_speaking),
            )
            if self.hotkeys.start():
                self._write(
                    f"Push to talk {config.VOICE.hotkey} · interrupt "
                    "ctrl+alt+space · quit ctrl+alt+q", "muted",
                )
            else:
                self._write(f"Hotkeys unavailable: {self.hotkeys.last_error}", "warn")
        except Exception as exc:  # noqa: BLE001
            self._write(f"Hotkeys unavailable: {type(exc).__name__}", "warn")

    def _start_scheduler(self) -> None:
        try:
            from ..triggers.scheduler import Nerves

            self.nerves = Nerves(
                notify=lambda text: self.bridge.events.put(
                    Event("background", {"text": text})
                )
            )
            if self.nerves.start():
                self._write(
                    f"Background jobs running: {', '.join(self.nerves.jobs())}",
                    "muted",
                )
        except Exception as exc:  # noqa: BLE001
            self._write(f"Scheduler unavailable: {type(exc).__name__}", "warn")

    # ------------------------------------------------------------ input
    def _start_wakeword(self) -> None:
        """Listen for the phrase the user recorded, if there is one."""
        if not config.VOICE.wake_enabled:
            return
        try:
            from ..voice.wakeword import wake
        except Exception as exc:  # noqa: BLE001
            self._write(f"Wake word unavailable: {type(exc).__name__}", "warn")
            return

        self.wake = wake
        if not wake.load():
            self._write(
                "No wake word recorded. Say one five times with /wake <phrase>.",
                "muted",
            )
            return

        # busy covers both thinking and speaking, which is what stops JARVIS
        # waking itself up on the sound of its own voice.
        if wake.start(
            on_wake=lambda: self.after(0, self._on_wake),
            is_busy=lambda: self.bridge.busy,
        ):
            label = wake.phrase or "your wake word"
            self._write(f'Listening for "{label}".', "muted")
        else:
            self._write(f"Wake word not listening: {wake.last_error}", "warn")

    def _on_wake(self) -> None:
        """The phrase was heard. Come to the front and start listening."""
        if self.bridge.busy:
            return
        self.show_window()
        self._write(f'"{self.wake.phrase or "wake word"}" - listening.', "muted")
        self.bridge.listen()

    def _enroll_wakeword(self, phrase: str) -> None:
        """Record the phrase five times and learn it.

        Runs on a worker thread because each recording blocks on the
        microphone, and Tk must keep painting the prompts while it does.
        """
        from ..voice.wakeword import REQUIRED_SAMPLES, wake

        wake.stop()

        def say(text: str, tag: str = "muted") -> None:
            self.after(0, lambda: self._write(text, tag))

        def worker() -> None:
            say(f'Recording "{phrase}". Say it {REQUIRED_SAMPLES} times, '
                "normally, with a pause between each.", "jarvis")
            clips = []
            for number in range(1, REQUIRED_SAMPLES + 1):
                say(f"  {number} of {REQUIRED_SAMPLES} - speak now")
                clip = wake.record_sample()
                if clip is None:
                    say(f"  heard nothing ({wake.last_error or 'silence'})", "warn")
                    continue
                clips.append(clip)
                say(f"  got {len(clip) / config.VOICE.sample_rate:.2f}s")

            result = wake.enroll_from_audio(clips, phrase=phrase)
            for problem in result.get("problems", []):
                say(f"  {problem}", "warn")
            if not result.get("ok"):
                say(result.get("message", "could not learn it"), "bad")
                return

            say(result["message"], "jarvis")
            if not result.get("consistent", True):
                say("  the recordings varied a lot; redo /wake if it misfires", "warn")
            self.after(0, self._start_wakeword)

        threading.Thread(target=worker, daemon=True, name="wake-enroll").start()

    def _submit(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        if self.bridge.busy:
            self.status.configure(text="still working on the last one")
            return

        self.entry.delete(0, "end")
        if text.startswith("/"):
            self._command(text)
            return

        self._write(f"you  {text}", "you")
        self.bridge.send(text)
        self._set_busy(True)

    def _command(self, raw: str) -> None:
        name = raw[1:].split()[0].lower() if len(raw) > 1 else ""
        agent = self.bridge.agent

        if name in ("quit", "exit"):
            self.quit_app()
        elif name == "clear":
            self.view.configure(state="normal")
            self.view.delete("1.0", "end")
            self.view.configure(state="disabled")
            if agent is not None:
                agent.ledger.clear()
            self._hide_banner()
            self._write("Cleared, including the taint flag.", "muted")
        elif name == "quota":
            for row in governor.by_kind():
                self._write(
                    f"  {row['kind']:<9} {row['requests']:>4} requests  "
                    f"{row['tokens']:>8,} tokens", "tool",
                )
        elif name == "memory":
            from ..memory.store import memory

            for fact in memory.all_facts(20):
                self._write(f"  [{fact.id}] {fact.fact}", "tool")
        elif name == "wake":
            parts = raw[1:].split(None, 1)
            argument = parts[1].strip() if len(parts) > 1 else ""
            if argument.lower() in ("off", "stop", "forget"):
                from ..voice.wakeword import wake

                wake.stop()
                wake.forget()
                self._write("Wake word forgotten; no longer listening.", "muted")
            else:
                self._enroll_wakeword(argument or "wake word")
        elif name == "voice":
            self.bridge.speak_replies = not self.bridge.speak_replies
            self._write(f"Speaking replies: {self.bridge.speak_replies}", "muted")
        else:
            self._write("Commands: /clear /quota /memory /voice /wake /quit", "muted")

    def _set_busy(self, busy: bool) -> None:
        self.send_button.configure(state="disabled" if busy else "normal")
        self.status.configure(text="thinking..." if busy else "ready")

    # ------------------------------------------------------------ events
    def _pump(self) -> None:
        for event in self.bridge.drain():
            try:
                self._handle(event)
            except Exception:  # noqa: BLE001 - a paint bug must not stop the pump
                pass
        self.after(80, self._pump)

    def _handle(self, event: Event) -> None:
        kind, data = event.kind, event.data

        if kind == "ready":
            self.status.configure(text="ready")
        elif kind == "turn_start":
            self.model_label.configure(
                text=f"{data.get('model')} · {data.get('tools')} tools "
                f"({data.get('profile')})"
            )
        elif kind == "tool_done":
            mark = "ok" if data.get("ok") else "failed"
            self._write(
                f"    {data.get('tool')} · {data.get('ms')}ms · {mark}",
                "ok" if data.get("ok") else "bad",
            )
        elif kind == "tool_judged" and data.get("outcome") in ("denied", "queued"):
            self._write(f"    {data.get('tool')} · {data.get('outcome')}", "warn")
        elif kind == "injection_detected":
            self._show_banner(
                f"Injected content detected in {data.get('tool')} — "
                f"{data.get('signatures')}. It was not acted on. Destructive "
                "actions now need your approval."
            )
            self._write(
                f"    injection detected in {data.get('tool')}: "
                f"{data.get('signatures')}", "bad",
            )
        elif kind == "approval_request":
            ApprovalDialog(
                self, data.get("tool", "?"), data.get("arguments", {}),
                data.get("reason", ""), self.bridge.approval.answer,
            )
        elif kind == "approval_timeout":
            self._write("    approval timed out, refused", "warn")
        elif kind == "listening":
            self._listening = bool(data.get("on"))
            self.mic_button.configure(
                text="Listening" if self._listening else "Talk",
                fg_color=ALARM if self._listening else FIELD,
            )
            self.status.configure(text="listening..." if self._listening else "ready")
        elif kind == "heard":
            self._write(f"you  {data.get('text')}", "you")
            self._set_busy(True)
        elif kind == "turn_done":
            self._write(data.get("reply", ""), "jarvis")
            bits = [f"{data.get('seconds')}s", f"{data.get('tokens')} tokens"]
            if data.get("denied"):
                bits.append(f"denied: {', '.join(data['denied'])}")
            if data.get("error"):
                bits.append(str(data["error"])[:60])
            self.status.configure(text="  ·  ".join(bits))
            self._set_busy(False)
        elif kind == "background":
            self._write(f"[background] {data.get('text')}", "warn")
        elif kind == "status":
            self.status.configure(text=str(data.get("text", "")))
        elif kind == "fatal":
            self._write(f"Something broke: {data.get('message')}", "bad")
            self._set_busy(False)

    # ------------------------------------------------------------ banner
    def _show_banner(self, text: str) -> None:
        self.banner_label.configure(text=text)
        self.banner.grid(row=1, column=0, columnspan=2, sticky="ew")

    def _hide_banner(self) -> None:
        self.banner.grid_forget()

    # ------------------------------------------------------------ status
    def _tick_status(self) -> None:
        try:
            snap = governor.snapshot()
            self.quota_label.configure(
                text=f"{snap.rpd_used}/{snap.rpd_limit} today · {snap.mode.value}",
                text_color=_MODE_COLOUR.get(snap.mode, MUTED),
            )
            agent = self.bridge.agent
            if agent is not None and agent.ledger.is_hostile:
                self.security_label.configure(text="INJECTION SEEN", text_color=ALARM)
            elif agent is not None and agent.ledger.is_tainted:
                self.security_label.configure(
                    text="untrusted content", text_color=WARN
                )
            else:
                self.security_label.configure(text="secure", text_color=GOOD)
        except Exception:  # noqa: BLE001
            pass
        self.after(2000, self._tick_status)

    # ------------------------------------------------------------ tray
    def attach_tray(self, tray) -> None:
        self._tray = tray

    def hide_to_tray(self) -> None:
        """Closing the window should not kill a background assistant."""
        if self._tray is not None and self._tray.running:
            self.withdraw()
            self.status.configure(text="running in the tray")
        else:
            self.quit_app()

    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_app(self) -> None:
        self.status.configure(text="shutting down...")
        try:
            if self.hotkeys:
                self.hotkeys.stop()
            if self.nerves:
                self.nerves.stop()
            if self._tray is not None:
                self._tray.stop()
            if getattr(self, 'wake', None) is not None:
                self.wake.stop()
            self.bridge.shutdown()
        finally:
            self.destroy()
