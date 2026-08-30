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
import re
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


# Labels that live in the chat header but are not the contact's name.
_HEADER_CHROME = {
    "voice call", "video call", "search", "menu", "profile details",
    "close", "back", "chat menu", "more options",
}


def _open_chat(contact: str) -> None:
    """Focus WhatsApp and open a contact's chat via the search box."""
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


def _open_chat_name(anchor) -> str:
    """The name on the chat header, read from around a header button."""
    node = anchor
    for _ in range(4):
        try:
            parent = node.GetParentControl()
        except Exception:  # noqa: BLE001
            break
        if parent is None:
            break
        node = parent
        for text in _texts_under(node, depth=3):
            if text.lower().strip() in _HEADER_CHROME or _TIMER.match(text):
                continue
            if len(text.strip()) >= 2:
                return text.strip()
    return ""


@tool(group="whatsapp")
def send_whatsapp(contact: str, message: str) -> dict:
    """Send a WhatsApp message to a contact by name.

    Drives the desktop app: focuses WhatsApp, searches for the contact, opens
    the first match and types the message. Get the name right -- it takes the
    top search result, so a vague name can open the wrong chat.

    The message is sent immediately and cannot be recalled. Report exactly
    what was sent and to whom.

    This is not a substitute for placing a call. If the user asked you to
    call someone, use call_whatsapp; do not send a message instead and
    report it as the next best thing.

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
    _open_chat(contact)

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


# --------------------------------------------------------------- call detection
# WhatsApp's window is a WebView: the whole interface is web content drawn
# *inside* the one top-level window. An incoming call is therefore usually not
# a new window at all, which is why looking for one found nothing. UI
# Automation can see into the web content and find the Decline button itself,
# which is both what a person would click and something that keeps working
# when the layout moves.
SEARCH_DEPTH = int(os.getenv("JARVIS_WHATSAPP_UIA_DEPTH", "28"))
DECLINE_RE = os.getenv(
    "JARVIS_WHATSAPP_DECLINE_RE", r"(?i)^(decline|reject|ignore|dismiss)\b"
)

# Labels that sit inside a call panel but are never the caller's name.
_NOT_A_NAME = {
    "decline", "reject", "ignore", "dismiss", "accept", "answer", "mute",
    "video", "voice", "speaker", "end call", "incoming call", "calling",
    "ringing", "whatsapp", "incoming voice call", "incoming video call",
}
_TIMER = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _uia():
    try:
        import uiautomation
    except ImportError:
        raise ToolError(
            "the uiautomation library is missing",
            hint="run: pip install uiautomation",
        ) from None
    # Without this every miss blocks for the default timeout, and a miss is
    # the common case -- most polls happen when nobody is calling.
    uiautomation.SetGlobalSearchTimeout(0)
    return uiautomation


_ROOT: dict = {"hwnd": 0, "control": None}


def _uia_root():
    """The WhatsApp window as a UIA control, cached between polls."""
    auto = _uia()
    hwnd = _main_window()
    if _ROOT["hwnd"] != hwnd or _ROOT["control"] is None:
        _ROOT["hwnd"], _ROOT["control"] = hwnd, auto.ControlFromHandle(hwnd)
    return _ROOT["control"]


def _texts_under(control, depth: int = 4, budget: int = 120) -> list[str]:
    """Names of the text elements below a control, nearest first."""
    out: list[str] = []
    stack = [(control, 0)]
    while stack and budget > 0:
        node, level = stack.pop(0)
        if level > depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:  # noqa: BLE001 - the tree changes while it is read
            continue
        for child in children:
            budget -= 1
            try:
                name = (child.Name or "").strip()
                kind = child.ControlTypeName
            except Exception:  # noqa: BLE001
                continue
            if name and kind in ("TextControl", "ButtonControl"):
                out.append(name)
            stack.append((child, level + 1))
    return out


def _caller_near(button) -> str:
    """Best guess at who is calling, read from the panel around the button."""
    node = button
    for _ in range(5):
        try:
            parent = node.GetParentControl()
        except Exception:  # noqa: BLE001
            break
        if parent is None:
            break
        node = parent
        for text in _texts_under(node):
            lowered = text.lower()
            if lowered in _NOT_A_NAME or _TIMER.match(text):
                continue
            if 2 <= len(text) <= 48:
                return text
    return ""


def find_incoming_call() -> dict | None:
    """Detect a ringing call, and return how to decline it.

    Returns a dict with the caller's name and the control to press, or None
    when nothing is ringing. Never raises: it runs on the scheduler thread
    every couple of seconds, and an exception there would stop the watcher.
    """
    auto = _uia()
    try:
        button = auto.ButtonControl(
            searchFromControl=_uia_root(),
            searchDepth=SEARCH_DEPTH,
            RegexName=DECLINE_RE,
        )
        if button.Exists(0, 0):
            return {"caller": _caller_near(button), "button": button, "via": "uia"}
    except Exception:  # noqa: BLE001 - a stale control invalidates the cache
        _ROOT["control"] = None

    # Older builds, and the tray case, do open a real call window.
    window = find_call_window()
    if window is not None:
        handle, title = window
        return {"caller": title, "handle": handle, "via": "window"}
    return None


def find_call_window() -> tuple[int, str] | None:
    """Return (handle, title) of an incoming-call window, if one is up.

    The main window is excluded by class, and invisible helper windows by
    visibility. What remains is a visible, titled window WhatsApp opened that
    is not the app itself -- which in practice means a call. The title usually
    carries the caller's name, which is what the watcher matches on.
    """
    try:
        main = _main_window()
    except ToolError:
        return None
    # The main window is excluded by *handle*. Excluding it by window class was
    # the original bug: a WinUI3 call window is the same framework and carries
    # the same class, so the filter meant to skip the main window skipped the
    # call window with it, and nothing was ever found.
    for handle, title, _cls, visible in _whatsapp_windows():
        if not visible or handle == main:
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
    call = find_incoming_call()
    if call is None:
        return {"declined": False, "note": "nothing is ringing"}

    if call["via"] == "uia":
        # Press the actual Decline button. Sending Escape at the window was
        # the old approach and it never reached the call at all, because the
        # call is drawn inside the main window rather than in one of its own.
        try:
            call["button"].Click(simulateMove=False)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(
                f"found the decline button but could not press it ({type(exc).__name__})",
                hint="decline it by hand this once, and report this",
            ) from None
        return {"declined": True, "caller": call["caller"] or "unknown", "how": "button"}

    _focus(call["handle"])
    _pyautogui().press("escape")
    time.sleep(0.3)
    return {"declined": True, "caller": call["caller"], "how": "escape"}


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


@tool(group="whatsapp")
def whatsapp_call_probe() -> dict:
    """Show what WhatsApp is displaying right now, for diagnosing calls.

    Run this **while a call is actually ringing**. It reports whether the
    decline button was found and what every button on screen is called, which
    is what identifies a call on this particular build. If declining is not
    working, this is the tool that says why.
    """
    auto = _uia()
    report: dict = {"windows": len(_whatsapp_windows())}

    call = find_incoming_call()
    report["ringing"] = call is not None
    if call is not None:
        report["caller"] = call["caller"] or "(name not found)"
        report["detected_via"] = call["via"]

    try:
        buttons = auto.ButtonControl(searchFromControl=_uia_root(), searchDepth=SEARCH_DEPTH)
        names, seen = [], set()
        for name in _texts_under(_uia_root(), depth=SEARCH_DEPTH, budget=900):
            if name not in seen and len(name) < 40:
                seen.add(name)
                names.append(name)
        report["on_screen"] = names[:60]
        del buttons
    except Exception as exc:  # noqa: BLE001
        report["on_screen_error"] = type(exc).__name__

    report["note"] = (
        "if ringing is false while a call is on screen, the decline button is "
        "named something this build does not expect -- look through on_screen "
        "for it and set JARVIS_WHATSAPP_DECLINE_RE in .env to match"
    )
    return report


@tool(group="whatsapp")
def call_whatsapp(contact: str, video: bool = False) -> dict:
    """Place a WhatsApp voice or video call to a contact by name.

    Use this whenever the user asks to call, ring or phone someone. It opens
    their chat and presses the call button, exactly as a person would.

    The call is placed immediately and the other person's phone rings, so get
    the name right. If the chat that opens belongs to somebody else this
    refuses to dial and says whose chat it found -- calling the wrong person
    is not a mistake that can be taken back.

    Args:
        contact: The contact name as it appears in WhatsApp.
        video: True for a video call, False for a voice call.
    """
    if not contact.strip():
        raise ToolError(
            "no contact given", hint="give the name as it appears in WhatsApp"
        )

    auto = _uia()
    wanted = "Video call" if video else "Voice call"
    _open_chat(contact)

    try:
        button = auto.ButtonControl(
            searchFromControl=_uia_root(), searchDepth=SEARCH_DEPTH, Name=wanted
        )
        found = button.Exists(0, 0)
    except Exception:  # noqa: BLE001 - a stale control invalidates the cache
        _ROOT["control"] = None
        found = False

    if not found:
        raise ToolError(
            f"could not find the {wanted} button",
            hint=(
                "the chat may not have opened -- check the contact name, or "
                "run whatsapp_call_probe to see what is on screen"
            ),
        )

    # Confirm the open chat is the person asked for before dialling. The
    # search box takes the top result, and for a call a wrong top result
    # means ringing a stranger rather than a recoverable typo.
    opened = _open_chat_name(button)
    wanted_l, opened_l = contact.strip().lower(), opened.lower()
    if opened and wanted_l not in opened_l and opened_l not in wanted_l:
        raise ToolError(
            f"searching for {contact!r} opened {opened!r} instead, so nothing was dialled",
            hint="use the name exactly as WhatsApp shows it, and try again",
        )

    button.Click(simulateMove=False)
    time.sleep(1.0)

    return {
        "calling": opened or contact,
        "kind": "video" if video else "voice",
        "note": "the call is ringing now; it was not answered by this tool",
    }
