"""Eyes.

This is the single biggest jump in feeling like JARVIS, and the easiest way
to set fire to a free-tier quota. Two rules make the difference:

1. **Vision is a subagent, not a main-loop step.** Capture, ask one narrow
   question, return a short text observation. The image never enters the
   conversation history. Put a screenshot in `history` and every subsequent
   turn re-sends it, and the tokens-per-minute ceiling arrives fast.

2. **Resolution decides the price.** "What does this error say" works fine at
   low and costs a fraction of high. Only reach for high when the detail
   genuinely matters -- dense text, small UI, fine print.
"""
from __future__ import annotations

import base64
import io

from ..client import ModelBlocked, QuotaExhausted, client
from ..config import Models
from ..prompts import VISION_SYSTEM
from ..quota import Mode, governor
from .base import ToolError, tool

# Downscale before sending. A 4K screenshot carries no more useful information
# for "read me that error" than a 1600px one, and costs several times as much.
MAX_WIDTH = 1600


def _downscale(png: bytes) -> bytes:
    """Shrink to MAX_WIDTH if Pillow is available; otherwise send as captured."""
    try:
        from PIL import Image
    except ImportError:
        return png

    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.width <= MAX_WIDTH:
                return png
            ratio = MAX_WIDTH / image.width
            resized = image.resize(
                (MAX_WIDTH, int(image.height * ratio)), Image.LANCZOS
            )
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a resize failure must not lose the screenshot
        return png


def _grab(monitor: int = 1) -> bytes:
    try:
        import mss
        import mss.tools
    except ImportError:
        raise ToolError("mss is not installed", hint="run: pip install mss") from None

    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor >= len(monitors):
            raise ToolError(
                f"no monitor {monitor}; this machine has {len(monitors) - 1}",
                hint="use 1 for the primary display",
            )
        shot = sct.grab(monitors[monitor])
        png = mss.tools.to_png(shot.rgb, shot.size)

    return _downscale(png)


@tool(group="vision", untrusted_output=True)
def see_screen(question: str, detail: str = "low", monitor: int = 1) -> dict:
    """Look at the user's screen and answer one question about it.

    Use for "what is this error", "read me the third row of that table",
    "which tab is the invoice one". Ask one specific question -- this returns
    a short observation, not a full description, and a vague question wastes
    the call.

    Do not use this to read a file you could read with read_file, and do not
    call it repeatedly to watch for changes; it costs quota every time.

    Args:
        question: The specific question to answer about the screen.
        detail: Image detail level -- low, medium, high, or ultra_high. Stay
            on low unless the text is genuinely too small to read.
        monitor: Which display to capture. 1 is the primary.
    """
    if detail not in ("low", "medium", "high", "ultra_high"):
        raise ToolError(
            f"unknown detail level: {detail}",
            hint="use low, medium, high, or ultra_high",
        )

    # Vision is the first capability to go when the budget is tight, because
    # it is the most expensive per unit of usefulness.
    snapshot = governor.snapshot()
    if snapshot.mode in (Mode.CONSERVE, Mode.CRITICAL, Mode.EXHAUSTED):
        raise ToolError(
            "vision is disabled while the daily quota is low",
            hint=(
                "tell the user you cannot look at the screen right now and ask "
                "them to describe what they see instead"
            ),
        )

    png = _grab(monitor)

    try:
        result = client.call(
            tier=Models.VISION,
            system_instruction=VISION_SYSTEM,
            input=[
                {"type": "text", "text": question},
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": base64.b64encode(png).decode(),
                    "resolution": detail,
                },
            ],
            kind="vision",
        )
    except QuotaExhausted as exc:
        raise ToolError(
            str(exc), hint="the daily budget is spent; try again tomorrow"
        ) from None
    except ModelBlocked as exc:
        raise ToolError(
            str(exc), hint="try a differently worded question about the screen"
        ) from None

    return {
        "observation": result.text,
        "detail_used": detail,
        "image_kb": round(len(png) / 1024, 1),
        "tokens": result.total_tokens,
    }
