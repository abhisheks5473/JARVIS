"""WhatsApp: sending messages and dealing with incoming calls.

Read this before relying on it.

**There is no API.** WhatsApp offers nothing programmatic for personal
accounts, so everything here drives the desktop app's interface: focus the
window, press keys, read window titles. That is not a robust foundation and
cannot be made into one. It works until WhatsApp changes its UI, and then it
needs fixing rather than tuning.

**It is against WhatsApp's terms.** Automating the client can get an account
banned. Occasional personal use is low risk; anything resembling bulk sending
is not. That is a real consequence to a real account, accepted deliberately.

**Windows are found by owning process, not by title.** The Store build is
WinUI3, and pygetwindow returns nothing for it: while it sits in the tray its
window is not visible and carries no discoverable title. Enumerating the
windows belonging to the WhatsApp process is the only approach that finds it.

**Keyboard, not pixels.** Ctrl+F opens search, the name goes in, Enter picks
the top result, the message goes in, Enter sends. No image matching, so the
realistic failure is "typed into the wrong chat" rather than "clicked
nothing" -- which is why the send tool reports the name it searched for, so a
wrong match is visible in the transcript instead of silent.
"""
from __future__ import annotations

import os
import time

from .base import ToolError, tool

# Long enough for the app to redraw between steps. Typing into this UI faster
# than it paints loses characters.
FOCUS_WAIT = 0.7
SEARCH_WAIT = 1.4
TYPE_INTERVAL = 0.02

# Words a call window's title has been seen to contain. Configurable because
# this is the part most likely to change, and editing .env beats editing code.
CALL_TITLE_HINTS = tuple(
    hint.strip().lower()
    for hint in os.getenv(
        "JARVIS_WHATSAPP_CALL_HINTS", "calling,incoming,ringing,call,video,voice"
    ).split(",")
    if hint.strip()
)


def _win32():
    try:
        import psutil
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        raise ToolError(
            f"the Windows automation libraries are missing ({exc.name})",
            hint="run: pip install pywin32 psutil",
        ) from None
    return win32gui, win32process, win32con, psutil


def _whatsapp_windows() -> list[tuple[int, str, str, bool]]:
    """Every window owned by WhatsApp: (handle, title, class, visible)."""
    win32gui, win32process, _win32con, psutil = _win32()
    found: list[tuple[int, str, str, bool]] = []

    def callback(handle, _extra):
        _, pid = win32process.GetWindowThreadProcessId(handle)
        try:
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 - process died mid-enumeration
            return
        if "whatsapp" in name.lower():
            found.append(
                (
                    handle,
                    win32gui.GetWindowText(handle),
                    win32gui.GetClassName(handle),
                    bool(win32gui.IsWindowVisible(handle)),
                )
            )

    win32gui.EnumWindows(callback, None)
    return found


def _main_window() -> int:
    """Handle of the real WhatsApp window, or an error that says what to do."""
    windows = _whatsapp_windows()
    if not windows:
        raise ToolError(
            "WhatsApp is not running",
            hint="launch it with launch_app('whatsapp'), then try again",
        )
    for handle, title, cls, _visible in windows:
        if "WinUIDesktopWin32WindowClass" in cls or title.strip() == "WhatsApp":
            return handle
    raise ToolError(
        "found the WhatsApp process but not its main window",
        hint="open WhatsApp from the tray so its window exists, then try again",
    )


def _focus(handle: int) -> None:
    """Bring a window to the front, restoring it if minimised."""
    win32gui, _win32process, win32con, _psutil = _win32()
    try:
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(handle)
    except Exception as exc:  # noqa: BLE001 - Windows refuses this sometimes
        raise ToolError(
            f"could not bring WhatsApp to the front ({type(exc).__name__})",
            hint="click WhatsApp once yourself, then ask again",
        ) from None
    time.sleep(FOCUS_WAIT)


def _pyautogui():
    try:
        import pyautogui
    except ImportError:
        raise ToolError(
            "pyautogui is not installed", hint="run: pip install pyautogui"
        ) from None
    pyautogui.FAILSAFE = True
    return pyautogui


@tool(group="whatsapp")
def send_whatsapp(contact: str, message: str) -> dict:
    """Send a WhatsApp message to a contact by name.

    Drives the desktop app: focuses WhatsApp, searches for the contact, opens
    the first match and types the message. Get the name right -- it takes the
    top search result, so a vague name can open the wrong chat.

    The message is sent immediately and cannot be recalled. Report exactly
    what was sent and to whom.

    Args:
        contact: The contact or group name as it appears in WhatsApp.
        message: The message to send.
    """
    if not contact.strip():
        raise ToolError(
            "no contact given", hint="give the name as it appears in WhatsApp"
        )
    if not message.strip():
        raise ToolError("no message given", hint="say what to send")
    if len(message) > 4000:
        raise ToolError(
            "that message is too long", hint="keep it under 4000 characters"
        )

    # Newlines send the message early in WhatsApp, so a multi-line message
    # would arrive as several fragments. Fold them rather than surprise anyone.
    text = " ".join(message.split())

    pyautogui = _pyautogui()
    _focus(_main_window())

    pyautogui.hotkey("ctrl", "f")          # focus the search box
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "a")          # clear whatever was there
    pyautogui.press("delete")
    pyautogui.write(contact, interval=TYPE_INTERVAL)
    time.sleep(SEARCH_WAIT)                # results need time to appear
    pyautogui.press("enter")               # open the top result
    time.sleep(0.8)

    pyautogui.write(text, interval=TYPE_INTERVAL)
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.4)

    return {
        "sent_to": contact,
        "message": text,
        "note": (
            "typed into whichever chat the search opened; if the name was "
            "ambiguous, say so rather than assuming it reached the right person"
        ),
    }


@tool(group="whatsapp")
def whatsapp_windows() -> dict:
    """List the windows WhatsApp currently has open.

    Use this to find out what an incoming call looks like on this machine:
    run it while a call is ringing, and the extra window is the call window.
    Also the quickest way to check WhatsApp is running at all.
    """
    windows = _whatsapp_windows()
    return {
        "running": bool(windows),
        "windows": [
            {"title": title, "class": cls, "visible": visible}
            for _handle, title, cls, visible in windows
        ],
        "count": len(windows),
    }


def find_call_window() -> tuple[int, str] | None:
    """Return (handle, title) of an incoming-call window, if one is up.

    The main window is excluded by class, and invisible helper windows by
    visibility. What remains is a visible, titled window WhatsApp opened that
    is not the app itself -- which in practice means a call. The title usually
    carries the caller's name, which is what the watcher matches on.
    """
    for handle, title, cls, visible in _whatsapp_windows():
        if not visible or "WinUIDesktopWin32WindowClass" in cls:
            continue
        stripped = title.strip()
        if not stripped or stripped == "WhatsApp":
            continue
        return handle, stripped
    return None


@tool(group="whatsapp")
def decline_whatsapp_call() -> dict:
    """Decline a WhatsApp call that is ringing right now.

    Only useful while a call is actually coming in. If nothing is ringing it
    says so rather than pressing keys into whatever happens to be focused.
    """
    pyautogui = _pyautogui()

    found = find_call_window()
    if found is None:
        return {"declined": False, "note": "no incoming call window is open"}

    handle, title = found
    _focus(handle)
    # Escape declines in current builds, and is the safest key to send at a
    # window whose layout cannot be inspected: the worst case is closing
    # something rather than answering a call by accident.
    pyautogui.press("escape")
    time.sleep(0.3)

    return {"declined": True, "window": title}


# The watcher lives in the scheduler because it must keep running between
# conversations. These tools are how it gets told what to do.
def _watcher():
    from ..triggers import scheduler

    watcher = getattr(scheduler, "_ACTIVE_CALL_WATCHER", None)
    if watcher is None:
        raise ToolError(
            "the background call watcher is not running",
            hint=(
                "it starts with the app; if JARVIS was launched with "
                "--no-scheduler, restart it without that flag"
            ),
        )
    return watcher


@tool(group="whatsapp")
def auto_decline_calls(contact: str, reply: str = "", enabled: bool = True) -> dict:
    """Automatically decline WhatsApp calls from someone, optionally replying.

    Set this up once and it keeps working in the background, between
    conversations, until it is turned off. Use it for "decline all calls from
    X and tell them Y".

    Args:
        contact: Whose calls to decline. Part of the name is enough, matched
            case-insensitively. Use "*" for everyone.
        reply: Optional message to send after declining. Leave empty to
            decline silently.
        enabled: False clears every rule and stops the watcher.
    """
    watcher = _watcher()

    if not enabled:
        watcher.clear_rules()
        return {"watching": False, "note": "all auto-decline rules cleared"}

    if not contact.strip():
        raise ToolError(
            "no name given", hint='give a name, or "*" to decline every call'
        )

    watcher.add_rule(contact, reply)
    return {
        "watching": True,
        "declining_calls_from": contact,
        "reply": reply or "(none)",
        "rules": len(watcher.rules),
        "note": (
            "this keeps running in the background until turned off with "
            "enabled=false"
        ),
    }


@tool(group="whatsapp")
def declined_calls() -> dict:
    """List the WhatsApp calls that were auto-declined.

    Use when the user asks who called, or whether the rule is working.
    """
    watcher = _watcher()
    import datetime

    return {
        "active_rules": [
            {"contact": name, "reply": reply or "(none)"}
            for name, reply in watcher.rules
        ],
        "declined": [
            {
                "caller": record["caller"],
                "at": datetime.datetime.fromtimestamp(record["at"]).strftime("%H:%M"),
                "replied": record["replied"],
            }
            for record in watcher.declined[-15:]
        ],
        "total_declined": len(watcher.declined),
    }
