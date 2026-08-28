"""The heads-up display.

Every other project of this name is a bare print loop, and that is not just an
aesthetic failure. A non-deterministic system you cannot see inside is a
system you debug by superstition. This panel shows, live: which tools fired
and how long they took, which model answered and whether it was downgraded,
the quota burn-down against all three limits, and the taint state of the
conversation.

It happens to also look the part, which is not nothing.

Everything degrades gracefully: if `rich` is missing or the terminal cannot
handle it, `PlainHUD` prints the same information as ordinary lines.
"""
from __future__ import annotations

import shutil

from ..quota import Mode, Snapshot
from ..security.taint import Level

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH = True
except ImportError:  # pragma: no cover - exercised only on a broken install
    RICH = False


_MODE_STYLE = {
    Mode.NORMAL: "green",
    Mode.CONSERVE: "yellow",
    Mode.CRITICAL: "dark_orange",
    Mode.EXHAUSTED: "red",
}
_TAINT_STYLE = {
    Level.CLEAN: ("green", "clean"),
    Level.SUSPECT: ("yellow", "untrusted content read"),
    Level.ACTIVE: ("bold red", "INJECTION SIGNATURES SEEN"),
}


def _bar(fraction: float, width: int = 18) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "#" * filled + "." * (width - filled)


class PlainHUD:
    """Fallback display. Same information, no formatting."""

    def __init__(self, name: str = "JARVIS") -> None:
        self.name = name

    def banner(self, lines: list[str]) -> None:
        width = min(shutil.get_terminal_size((80, 24)).columns, 78)
        print("=" * width)
        for line in lines:
            print(line)
        print("=" * width)

    def event(self, kind: str, data: dict) -> None:
        if kind == "turn_start":
            print(
                f"  [{data.get('model')} | {data.get('tools')} tools | "
                f"{data.get('profile')}]"
            )
        elif kind == "tool_judged" and data.get("outcome") != "allow":
            print(f"  ! {data.get('tool')}: {data.get('outcome')}")
        elif kind == "tool_done":
            mark = "ok" if data.get("ok") else "FAILED"
            print(f"  - {data.get('tool')} ({data.get('ms')}ms) {mark}")
        elif kind == "injection_detected":
            print(f"  !! INJECTION in {data.get('tool')}: {data.get('signatures')}")
        elif kind == "quota_exhausted":
            print(f"  !! quota: {data.get('reason')}")

    def reply(self, text: str) -> None:
        print(f"\n{self.name}> {text}\n")

    def user_echo(self, text: str) -> None:
        print(f"you> {text}")

    def approval(self, tool: str, arguments: dict, reason: str) -> bool:
        print(f"\n  APPROVAL REQUIRED: {tool}")
        for key, value in arguments.items():
            print(f"    {key} = {str(value)[:300]}")
        print(f"    reason: {reason}")
        try:
            return input("    allow? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def status(self, snapshot: Snapshot, taint: Level, extra: str = "") -> None:
        tail = f" | {extra}" if extra else ""
        print(
            f"  [quota {snapshot.rpd_used}/{snapshot.rpd_limit} today | "
            f"{snapshot.mode.value} | taint {taint.name}{tail}]"
        )

    def note(self, text: str) -> None:
        print(f"  {text}")


class HUD(PlainHUD):
    """Rich terminal display."""

    def __init__(self, name: str = "JARVIS") -> None:
        super().__init__(name)
        self.console = Console()

    # ------------------------------------------------------------ chrome
    def banner(self, lines: list[str]) -> None:
        body = Group(*[Text(line) for line in lines])
        self.console.print(
            Panel(
                body,
                title=f"[bold cyan]{self.name}[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def note(self, text: str) -> None:
        self.console.print(f"  [dim]{text}[/dim]")

    def user_echo(self, text: str) -> None:
        self.console.print(f"[bold white]you[/bold white] [dim]>[/dim] {text}")

    # ------------------------------------------------------------ events
    def event(self, kind: str, data: dict) -> None:
        if kind == "turn_start":
            self.console.print(
                f"  [dim]{data.get('model')} · thinking {data.get('thinking')} · "
                f"{data.get('tools')} tools ({data.get('profile')})[/dim]"
            )
        elif kind == "tool_judged":
            outcome = data.get("outcome")
            if outcome == "denied":
                self.console.print(f"  [red]denied[/red] {data.get('tool')}")
            elif outcome == "queued":
                self.console.print(f"  [yellow]queued[/yellow] {data.get('tool')}")
            elif data.get("escalated"):
                self.console.print(
                    f"  [yellow]escalated[/yellow] {data.get('tool')} "
                    "[dim](conversation is tainted)[/dim]"
                )
        elif kind == "tool_done":
            style = "green" if data.get("ok") else "red"
            mark = "OK" if data.get("ok") else "XX"
            self.console.print(
                f"  [{style}]{mark}[/{style}] [cyan]{data.get('tool')}[/cyan] "
                f"[dim]{data.get('ms')}ms[/dim]"
            )
        elif kind == "injection_detected":
            self.console.print(
                Panel(
                    Text(
                        f"Content from {data.get('tool')} matched injection "
                        f"signatures: {data.get('signatures')} "
                        f"(score {data.get('score')}).\n"
                        "Destructive tools now require your explicit approval "
                        "for the rest of this conversation.",
                        style="bold red",
                    ),
                    title="[bold red]PROMPT INJECTION DETECTED[/bold red]",
                    border_style="red",
                )
            )
        elif kind == "compacted":
            self.console.print(
                f"  [dim]history compacted, {data.get('kept')} entries kept[/dim]"
            )
        elif kind == "quota_exhausted":
            self.console.print(f"  [bold red]quota:[/bold red] {data.get('reason')}")
        elif kind == "error":
            self.console.print(f"  [red]error:[/red] {data.get('message')}")

    def reply(self, text: str) -> None:
        self.console.print(
            Panel(
                Text(text, style="white"),
                title=f"[bold cyan]{self.name}[/bold cyan]",
                border_style="cyan",
                padding=(0, 1),
            )
        )

    # ------------------------------------------------------------ approval
    def approval(self, tool: str, arguments: dict, reason: str) -> bool:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style="dim", justify="right")
        table.add_column(style="bold white")
        for key, value in arguments.items():
            table.add_row(key, str(value)[:400])

        hostile = "INJECTION RISK" in reason
        self.console.print(
            Panel(
                Group(Text(reason, style="bold red" if hostile else "yellow"), table),
                title=f"[bold yellow]approve {tool}?[/bold yellow]",
                border_style="red" if hostile else "yellow",
            )
        )
        try:
            answer = self.console.input("  [bold]allow?[/bold] [dim][y/N][/dim] ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")

    # ------------------------------------------------------------ status
    def status(self, snapshot: Snapshot, taint: Level, extra: str = "") -> None:
        style = _MODE_STYLE.get(snapshot.mode, "white")
        taint_style, taint_label = _TAINT_STYLE.get(taint, ("white", "unknown"))

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="dim", justify="right")
        table.add_column()

        table.add_row(
            "day",
            f"[{style}]{_bar(snapshot.rpd_used / max(snapshot.rpd_limit, 1))}[/{style}]"
            f"  {snapshot.rpd_used}/{snapshot.rpd_limit} requests"
            f"  [dim]resets in {snapshot.resets_in_s / 3600:.1f}h[/dim]",
        )
        table.add_row(
            "minute",
            f"{_bar(snapshot.rpm_used / max(snapshot.rpm_limit, 1), 10)}"
            f"  {snapshot.rpm_used}/{snapshot.rpm_limit} rpm"
            f"  ·  {snapshot.tpm_used:,}/{snapshot.tpm_limit:,} tpm",
        )
        table.add_row("tokens", f"{snapshot.tokens_today:,} today")
        table.add_row("mode", f"[{style}]{snapshot.mode.value}[/{style}]")
        table.add_row("security", f"[{taint_style}]{taint_label}[/{taint_style}]")
        if extra:
            table.add_row("", f"[dim]{extra}[/dim]")

        self.console.print(
            Panel(table, title="[dim]status[/dim]", border_style="dim", padding=(0, 1))
        )


def make_hud(name: str = "JARVIS", enabled: bool = True) -> PlainHUD:
    """Pick the best display this terminal can actually render."""
    if enabled and RICH:
        try:
            return HUD(name)
        except Exception:  # noqa: BLE001 - fall back rather than fail to start
            return PlainHUD(name)
    return PlainHUD(name)
