"""Machine state: what is running, what is hot, what is full.

Read-only apart from `kill_process`, which is gated like everything else that
cannot be undone.
"""
from __future__ import annotations

import platform
import shutil
import time
from pathlib import Path

from .base import ToolError, tool


def _psutil():
    try:
        import psutil
    except ImportError:
        raise ToolError(
            "psutil is not installed", hint="run: pip install psutil"
        ) from None
    return psutil


def _gb(value: float) -> float:
    return round(value / (1024**3), 1)


@tool(group="system")
def system_stats() -> dict:
    """Report CPU, memory, disk and battery status.

    Use when the user asks why the machine is slow, how much space is left,
    or how long the battery will last.
    """
    psutil = _psutil()

    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(Path.home().anchor or "C:\\")

    stats: dict = {
        "cpu_percent": psutil.cpu_percent(interval=0.4),
        "cpu_cores": psutil.cpu_count(logical=True),
        "memory_used_gb": _gb(memory.used),
        "memory_total_gb": _gb(memory.total),
        "memory_percent": memory.percent,
        "disk_free_gb": _gb(disk.free),
        "disk_total_gb": _gb(disk.total),
        "disk_percent_used": round(100 * (disk.used / disk.total), 1),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "platform": f"{platform.system()} {platform.release()}",
    }

    battery = getattr(psutil, "sensors_battery", lambda: None)()
    if battery is not None:
        stats["battery_percent"] = round(battery.percent)
        stats["battery_plugged_in"] = bool(battery.power_plugged)
        if battery.secsleft and battery.secsleft > 0:
            stats["battery_hours_left"] = round(battery.secsleft / 3600, 1)

    return stats


@tool(group="system")
def list_processes(sort_by: str = "memory", limit: int = 12) -> dict:
    """List the heaviest running processes.

    Use this to answer "what is eating my RAM", or to find the exact process
    name and PID before killing something.

    Args:
        sort_by: Either memory or cpu.
        limit: How many processes to return.
    """
    if sort_by not in ("memory", "cpu"):
        raise ToolError("sort_by must be memory or cpu", hint="use one of those two")

    psutil = _psutil()
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            memory_mb = (
                round(info["memory_info"].rss / (1024**2), 1)
                if info.get("memory_info")
                else 0.0
            )
            rows.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "?",
                    "memory_mb": memory_mb,
                    "cpu_percent": info.get("cpu_percent") or 0.0,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = "memory_mb" if sort_by == "memory" else "cpu_percent"
    rows.sort(key=lambda r: r[key], reverse=True)
    return {"processes": rows[: max(1, min(int(limit), 40))], "sorted_by": sort_by}


@tool(group="system")
def kill_process(pid: int) -> dict:
    """Terminate a process by its numeric PID.

    Call list_processes first to get the PID -- never guess one. Unsaved work
    in that process is lost, so this requires the user's approval.

    Args:
        pid: The process ID to terminate.
    """
    psutil = _psutil()
    try:
        proc = psutil.Process(int(pid))
        name = proc.name()
        proc.terminate()
        proc.wait(timeout=5)
    except psutil.NoSuchProcess:
        raise ToolError(
            f"no process with pid {pid}",
            hint="call list_processes for current pids; they change constantly",
        ) from None
    except psutil.AccessDenied:
        raise ToolError(
            f"access denied terminating pid {pid}",
            hint=(
                "this is likely a system process; tell the user it needs "
                "administrator rights"
            ),
        ) from None
    except psutil.TimeoutExpired:
        return {
            "pid": pid,
            "terminated": False,
            "note": "did not exit within 5 seconds",
        }

    return {"pid": pid, "name": name, "terminated": True}


@tool(group="dev", untrusted_output=True)
def read_log_tail(path: str, lines: int = 40) -> dict:
    """Read the last few lines of a log file anywhere on disk.

    Use for debugging when the user mentions an error in a specific log.
    Unlike read_file this is not restricted to the workspace, so it returns
    only the tail and never the whole file.

    Args:
        path: Full path to the log file.
        lines: How many trailing lines to return. Maximum 200.
    """
    from ..security.approval import redact
    from .files import _is_sensitive

    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError:
        raise ToolError(f"unusable path: {path}", hint="give a full path") from None

    # This tool deliberately reads outside the workspace roots, which is the
    # point of it -- logs live in odd places. That made it the one file tool
    # with no boundary at all, and an audit found it happily returning the
    # contents of .env. The credential denylist applies here too.
    reason = _is_sensitive(target)
    if reason:
        raise ToolError(
            f"refusing to read that file -- {reason}",
            hint="credential files are off-limits; tell the user rather than retrying",
        )

    if not target.is_file():
        raise ToolError(f"no such file: {target}", hint="check the full path")

    count = max(1, min(int(lines), 200))
    try:
        content = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ToolError(f"could not read {target}: {exc}", hint="") from None

    # Logs are a classic place for a token to end up in plain text.
    tail = [redact(line) for line in content[-count:]]
    return {
        "path": str(target),
        "lines": tail,
        "total_lines": len(content),
        "showing_last": len(tail),
    }
