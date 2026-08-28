"""Mouse and keyboard: driving the machine directly.

This is the most powerful thing in the toolbox and, with approval prompts
switched off, the least supervised. Three consequences shaped the design:

1. **The corner is the kill switch.** pyautogui's failsafe is left ON, so
   slamming the pointer into the top-left corner aborts whatever it is doing
   mid-action. That is now the fastest physical way to stop a runaway
   sequence, and it works even when the terminal is not focused.

2. **Clicking blind is worse than not clicking.** The screen is not a
   coordinate space the model knows anything about. Every description here
   pushes it to call see_screen first and click on what it has actually
   observed, because a confident click at the wrong coordinates lands on
   whatever happens to be there -- a Delete button, a Send button, a Confirm
   dialog.

3. **Coordinates are validated, not clamped.** Off-screen values are refused,
   because a clamped click is a click somewhere the model did not intend.

Typing lives here rather than in desktop.py because it is the same "pretend
to be the user" capability, and carries the same warning: text goes to
whatever holds focus, which is not necessarily what the model believes holds
focus.
"""
from __future__ import annotations

from .base import ToolError, tool

# An allowlist, so a typo becomes a clear error rather than a silently
# ignored keystroke.
_KEYS = {
    "enter", "return", "tab", "escape", "esc", "space", "backspace", "delete",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "ctrl", "alt", "shift", "win", "capslock", "insert", "printscreen",
}
_BUTTONS = {"left", "right", "middle"}


def _gui():
    try:
        import pyautogui
    except ImportError:
        raise ToolError(
            "pyautogui is not installed", hint="run: pip install pyautogui"
        ) from None

    # The failsafe is the physical abort: shove the pointer into the top-left
    # corner and the next call raises. Never turn this off.
    pyautogui.FAILSAFE = True
    # A short pause between actions keeps the target application able to keep
    # up; without it, clicks land before the UI has redrawn.
    pyautogui.PAUSE = 0.08
    return pyautogui


def _check_point(pyautogui, x, y) -> tuple[int, int]:
    width, height = pyautogui.size()
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        raise ToolError(
            f"coordinates must be whole numbers, got ({x!r}, {y!r})",
            hint="call screen_info for the screen size, then give pixel values",
        ) from None

    if not (0 <= x < width and 0 <= y < height):
        raise ToolError(
            f"({x}, {y}) is off screen; this display is {width} by {height}",
            hint=(
                "call screen_info first, and see_screen to find what you are "
                "aiming at; do not guess coordinates"
            ),
        )
    return x, y


@tool(group="mouse")
def screen_info() -> dict:
    """Get the screen size and where the pointer currently is.

    Call this before any mouse action so you are working in real coordinates
    rather than assumed ones.
    """
    pyautogui = _gui()
    width, height = pyautogui.size()
    x, y = pyautogui.position()
    return {
        "width": width,
        "height": height,
        "mouse_x": x,
        "mouse_y": y,
        "centre": [width // 2, height // 2],
    }


@tool(group="mouse")
def move_mouse(x: int, y: int, duration: float = 0.2) -> dict:
    """Move the pointer to a screen coordinate without clicking.

    Useful for hovering to reveal a tooltip or a menu. Call see_screen first
    if you are not certain what is at that position.

    Args:
        x: Horizontal pixel, 0 is the left edge.
        y: Vertical pixel, 0 is the top edge.
        duration: Seconds the movement takes. A little travel time helps
            applications that react to hover.
    """
    pyautogui = _gui()
    x, y = _check_point(pyautogui, x, y)
    pyautogui.moveTo(x, y, duration=max(0.0, min(float(duration), 3.0)))
    return {"moved_to": [x, y]}


@tool(group="mouse")
def click_mouse(x: int = -1, y: int = -1, button: str = "left", clicks: int = 1) -> dict:
    """Click the mouse, optionally moving somewhere first.

    Look before you click. Call see_screen to confirm what is at the position
    you are about to hit -- a click at the wrong coordinates lands on whatever
    is there, which may be a Delete, Send or Confirm button.

    Omit x and y to click wherever the pointer already is.

    Args:
        x: Horizontal pixel. Leave at -1 to click the current position.
        y: Vertical pixel. Leave at -1 to click the current position.
        button: left, right, or middle.
        clicks: 1 for a single click, 2 for a double click.
    """
    pyautogui = _gui()
    if button not in _BUTTONS:
        raise ToolError(f"unknown button: {button}", hint="use left, right, or middle")
    count = max(1, min(int(clicks), 3))

    # Only -1 means "where the pointer already is". Any other negative is
    # arithmetic that went wrong, and treating it as the sentinel would click
    # at the current position instead of failing -- which is precisely the
    # unintended click this validation exists to prevent.
    for axis, value in (("x", x), ("y", y)):
        if value < -1:
            raise ToolError(
                f"{axis}={value} is not a valid coordinate",
                hint="use -1 to click the current position, or a real pixel value",
            )

    if x >= 0 and y >= 0:
        x, y = _check_point(pyautogui, x, y)
        pyautogui.click(x=x, y=y, clicks=count, button=button, interval=0.08)
        where = [x, y]
    else:
        pyautogui.click(clicks=count, button=button, interval=0.08)
        where = list(pyautogui.position())

    return {"clicked": where, "button": button, "clicks": count}


@tool(group="mouse")
def drag_mouse(
    from_x: int, from_y: int, to_x: int, to_y: int, duration: float = 0.5
) -> dict:
    """Press the left button at one point, drag to another, and release.

    For moving a file, selecting a region, or repositioning a window. Confirm
    both ends with see_screen first.

    Args:
        from_x: Starting horizontal pixel.
        from_y: Starting vertical pixel.
        to_x: Ending horizontal pixel.
        to_y: Ending vertical pixel.
        duration: Seconds the drag takes. Too fast and some applications drop
            the gesture entirely.
    """
    pyautogui = _gui()
    from_x, from_y = _check_point(pyautogui, from_x, from_y)
    to_x, to_y = _check_point(pyautogui, to_x, to_y)

    pyautogui.moveTo(from_x, from_y, duration=0.15)
    pyautogui.dragTo(
        to_x, to_y, duration=max(0.1, min(float(duration), 5.0)), button="left"
    )
    return {"dragged_from": [from_x, from_y], "to": [to_x, to_y]}


@tool(group="mouse")
def scroll_mouse(
    amount: int = 3, direction: str = "down", x: int = -1, y: int = -1
) -> dict:
    """Scroll the wheel, optionally over a specific position.

    Scrolling applies to whatever is under the pointer, so move there first if
    the window you mean is not already under it.

    Args:
        amount: How many wheel notches. Three is about one comfortable step.
        direction: up or down.
        x: Optional horizontal pixel to scroll over.
        y: Optional vertical pixel to scroll over.
    """
    pyautogui = _gui()
    if direction not in ("up", "down"):
        raise ToolError("direction must be up or down", hint="use one of those two")

    notches = max(1, min(int(amount), 25))
    if x >= 0 and y >= 0:
        x, y = _check_point(pyautogui, x, y)
        pyautogui.moveTo(x, y, duration=0.1)

    pyautogui.scroll(notches * (1 if direction == "up" else -1))
    return {"scrolled": direction, "notches": notches}


@tool(group="mouse")
def type_text(text: str, interval: float = 0.02) -> dict:
    """Type text into whatever currently has keyboard focus.

    Confirm the right field is focused first, with see_screen or by clicking
    it. Text goes wherever focus actually is, which is not always where you
    believe it is.

    Never type passwords, API keys, or 2FA codes with this. If asked to,
    decline and let the user type it themselves.

    Args:
        text: The text to type.
        interval: Seconds between keystrokes. Raise it for applications that
            drop fast input.
    """
    if not text:
        raise ToolError("nothing to type", hint="give some text")
    if len(text) > 5000:
        raise ToolError(
            "that is too much to type in one go",
            hint="use write_file for long content, or split it up",
        )

    pyautogui = _gui()
    pyautogui.write(text, interval=max(0.0, min(float(interval), 0.5)))
    return {"typed_chars": len(text)}


@tool(group="mouse")
def press_keys(keys: str) -> dict:
    """Press a key or a keyboard shortcut.

    Give a single key like "enter", or a combination joined by plus signs such
    as "ctrl+s" or "alt+tab". Prefer this over hunting for buttons with the
    mouse -- it is faster and far more reliable.

    Args:
        keys: A key name, or a combination such as "ctrl+shift+t".
    """
    pyautogui = _gui()
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    if not parts:
        raise ToolError("no keys given", hint='try "enter" or "ctrl+s"')

    for part in parts:
        if part not in _KEYS and len(part) != 1:
            raise ToolError(
                f"unknown key: {part}",
                hint="use single characters, or a named key such as enter or tab",
            )

    if len(parts) == 1:
        pyautogui.press(parts[0])
    else:
        pyautogui.hotkey(*parts)
    return {"pressed": "+".join(parts)}
