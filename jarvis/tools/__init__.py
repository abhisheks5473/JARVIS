"""Tool registry index.

Importing this module is what registers every tool -- the `@tool` decorators
run at import time. Adding a tool file means adding one import here and
nothing else.

The catalogue is larger than the guide's recommended 10-20 active tools, which
is fine only because the registry offers a *subset* per turn. Beyond roughly
twenty declarations, selection accuracy drops noticeably: the model starts
picking plausible-but-wrong tools. `PROFILES` below is how the offered set
stays small while the catalogue stays useful.
"""
from __future__ import annotations

import os

from .base import Registry, ToolError, ToolSpec, registry, tool

# Import for side effects: each module registers its tools on import.
from . import desktop  # noqa: F401,E402
from . import files  # noqa: F401,E402
from . import google_ws  # noqa: F401,E402
from . import memory_tools  # noqa: F401,E402
from . import mouse  # noqa: F401,E402
from . import shell  # noqa: F401,E402
from . import system  # noqa: F401,E402
from . import time_tools  # noqa: F401,E402
from . import vision  # noqa: F401,E402
from . import web  # noqa: F401,E402

# Gemini's built-in server-side tools.
#
# DISABLED BY DEFAULT, and this is not caution -- it is measured. Passing
# {"type": "google_search"} on a free-tier key returns 429 immediately, on
# every call, while the identical request without it succeeds before and
# after. Search grounding is not included in the free tier, whatever the
# docs imply. Leaving it on makes every single agent turn fail.
#
# `web_search` in tools/web.py fills the gap using DuckDuckGo's HTML
# endpoint: no key, no quota, no billing.
#
# Set JARVIS_GOOGLE_SEARCH=1 once you have enabled billing, then verify with
# `python -m jarvis.doctor`, which probes this specific capability.
BUILTIN_TOOLS: list[dict] = (
    [{"type": "google_search"}]
    if os.getenv("JARVIS_GOOGLE_SEARCH", "0") == "1"
    else []
)

# Named tool sets, listed by tool name rather than by group.
#
# Groups are too coarse for this: activating "files" wholesale drags trash
# management and move_path into every single turn, and the default loadout
# drifts past twenty tools -- exactly where selection accuracy starts to
# slide. Every profile below is deliberately kept at or under twenty.
#
# get_time, remember and search_memory are marked always_available, so they
# are offered on top of these sets without being listed each time.
PROFILES: dict[str, set[str]] = {
    # The everyday loadout: 17 tools plus the 3 always-available ones.
    "default": {
        "read_file", "write_file", "list_directory", "search_files", "delete_path",
        "web_search", "fetch_url",
        "see_screen",
        "launch_app", "list_windows", "focus_window", "media_control",
        "read_clipboard", "send_notification",
        "system_stats",
        "date_math",
    },
    # Conversation only. For when quota is nearly gone.
    "minimal": set(),
    # Driving the machine.
    "desk": {
        "launch_app", "list_windows", "focus_window", "close_window",
        "media_control", "set_volume", "read_clipboard", "set_clipboard",
        "send_notification", "see_screen",
        "system_stats", "list_processes",
        "screen_info", "click_mouse", "press_keys",
    },
    # Point-and-click work. see_screen is the important one here: clicking
    # without looking first is how an agent hits the wrong button.
    "control": {
        "see_screen", "screen_info", "move_mouse", "click_mouse", "drag_mouse",
        "scroll_mouse", "type_text", "press_keys",
        "list_windows", "focus_window", "launch_app", "read_clipboard",
    },
    # Reading and writing about the world.
    "research": {
        "web_search", "fetch_url", "read_file", "write_file", "list_directory", "search_files",
        "see_screen", "date_math",
    },
    # Working on code.
    "dev": {
        "read_file", "write_file", "list_directory", "search_files", "delete_path",
        "move_path", "git_command", "read_log_tail", "run_powershell", "run_shell",
        "system_stats", "list_processes", "see_screen",
    },
    # The morning briefing job.
    "briefing": {"list_calendar", "read_email", "web_search", "fetch_url",
                 "send_notification"},
    # Recovering something deleted.
    "recovery": {"list_trash", "restore_from_trash", "list_directory", "read_file"},
}


def profile_tools(name: str = "default", extra: set[str] | None = None) -> set[str]:
    """The tool names for a profile, WITHOUT mutating the registry.

    Use this on any request path. `use_profile` mutates process-wide state and
    is only safe for the single-threaded REPL, because a background job on the
    scheduler thread would otherwise redefine the toolset mid-turn.
    """
    if name == "everything":
        return set(registry.names())
    return set(PROFILES.get(name, PROFILES["default"])) | (extra or set())


def use_profile(name: str = "default", extra: set[str] | None = None) -> list[str]:
    """Activate a named tool set and return the names now offered.

    `extra` is unioned in on top of the profile. Profiles are a floor, not a
    ceiling: choosing one must never remove a capability the request plainly
    needs. See router.required_tools.
    """
    if name == "everything":
        registry.activate(None)
    else:
        names = set(PROFILES.get(name, PROFILES["default"]))
        registry.activate_names(names | (extra or set()))
    return sorted(spec.name for spec in registry.active())


def catalogue() -> dict[str, list[str]]:
    """Every registered tool, grouped. For the doctor and the README."""
    grouped: dict[str, list[str]] = {}
    for name in registry.names():
        spec = registry.get(name)
        if spec is not None:
            grouped.setdefault(spec.group, []).append(spec.name)
    return {group: sorted(names) for group, names in sorted(grouped.items())}


__all__ = [
    "BUILTIN_TOOLS",
    "PROFILES",
    "Registry",
    "ToolError",
    "ToolSpec",
    "catalogue",
    "profile_tools",
    "registry",
    "tool",
    "use_profile",
]
