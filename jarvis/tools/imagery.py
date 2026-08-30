"""Generating pictures and video from a description.

Distinct from `media.py`, which *assembles* media -- images into a slideshow,
one container into another, a photo resized. This asks a model for something
that did not exist before.

**Neither is free, and that was measured rather than assumed.** Both model
families are listed by the API and both return 429 RESOURCE_EXHAUSTED on a
free-tier key. The check that makes this meaningful is the bisection: a text
request on the same key, in the same second, immediately before and after,
succeeds. So the 429 is the tier, not a rate limit and not a busy moment --
exactly the shape of the `google_search` finding, and it would otherwise look
like an intermittent failure worth retrying forever.

The tools are wired to the real API anyway, because the block is Google's
billing rather than anything here: enable billing on the key's project and
they work with no code change. What they must never do is fail vaguely. A
generic "quota exceeded" would send someone hunting for a bug in their own
prompt, so the error says plainly which tier is missing and what to do.

**Video is the one path never observed working.** Image generation was at
least exercised far enough to see the request reach the model and be refused
for billing. Veo could not be run to completion here, so the polling and
download below follow the SDK's documented shapes and are, honestly,
unverified. Anything surprising about them should be treated as a bug here
first.
"""
from __future__ import annotations

import time

from .. import config
from .base import ToolError, tool
from .files import _relative, _resolve

IMAGE_FORMATS = ("png", "jpg", "jpeg", "webp")
VIDEO_FORMATS = ("mp4", "mkv", "webm")

# Veo takes a while, and a tool that returns before the file exists is worse
# than one that waits: the agent reports success and the user finds nothing.
VIDEO_POLL_S = 10
VIDEO_TIMEOUT_S = 480


def _client():
    import os

    from google import genai

    key = os.getenv(config.API_KEY_ENV, "").strip()
    if not key:
        raise ToolError(
            "no API key is set",
            hint=f"put {config.API_KEY_ENV} in your .env",
        )
    return genai.Client(api_key=key)


def _target(path: str, fmt: str):
    candidate = path if path.lower().endswith(f".{fmt}") else f"{path}.{fmt}"
    target = _resolve(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# Once a model has refused for billing, it will keep refusing. Remembering
# that for a few minutes makes the retry instant and free instead of another
# round trip -- and the model does retry, observed twice in a row on the very
# first run despite the error saying not to.
_REFUSED: dict[str, float] = {}
REFUSAL_MEMORY_S = 600


def _refused_recently(model: str) -> bool:
    when = _REFUSED.get(model)
    return when is not None and time.time() - when < REFUSAL_MEMORY_S


def _not_on_free_tier(what: str, model: str) -> ToolError:
    """The error to raise when the tier, not the request, is the problem."""
    _REFUSED[model] = time.time()
    return ToolError(
        f"{what} is not included in the Gemini free tier",
        hint=(
            "the key answers text requests fine at the same moment, so this "
            "is the tier rather than a rate limit. DO NOT RETRY -- it will "
            "fail identically until billing is enabled on the project behind "
            f"the API key, which is what {model} needs. Tell the user that, "
            "and offer the free media tools instead: create_document, "
            "create_audio, or create_video from images they already have"
        ),
    )


def _explain(exc: Exception, what: str, model: str) -> ToolError:
    message = str(exc)
    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return _not_on_free_tier(what, model)
    if "404" in message or "NOT_FOUND" in message:
        return ToolError(
            f"the model {model} is not available to this key",
            hint=(
                "Google retires model IDs on their own schedule -- run "
                "'python -m jarvis.doctor' to see what your key actually has, "
                "then set JARVIS_IMAGE_MODEL or JARVIS_VIDEO_MODEL in .env"
            ),
        )
    if "403" in message or "PERMISSION_DENIED" in message:
        return ToolError(
            f"your key is not permitted to use {model}",
            hint="check the API is enabled on the key's Google Cloud project",
        )
    if "SAFETY" in message.upper() or "blocked" in message.lower():
        return ToolError(
            "the model refused that prompt",
            hint="rephrase it, or describe the picture without naming people",
        )
    return ToolError(
        f"{what} failed: {type(exc).__name__}: {message[:160]}",
        hint="try a simpler prompt, or check the network",
    )


@tool(group="imagery")
def generate_image(
    prompt: str, path: str, format: str = "png", reference_image: str = ""
) -> dict:
    """Generate a picture from a description, and save it.

    Ask for what should be in the image, not for a file: "a watercolour fox in
    snow" rather than "make me a png". Give a reference image to edit an
    existing picture instead -- "make this photo look like winter" -- which
    leaves the original untouched and writes a new file.

    Args:
        prompt: What the picture should show.
        path: Where to save it, e.g. "Desktop/fox".
        format: png, jpg or webp.
        reference_image: Optional existing image to edit rather than start blank.
    """
    if not prompt.strip():
        raise ToolError("no prompt given", hint="describe the picture you want")

    fmt = format.lower().lstrip(".")
    if fmt not in IMAGE_FORMATS:
        raise ToolError(
            f"unknown image format: {format}",
            hint=f"use one of {', '.join(IMAGE_FORMATS)}",
        )

    model = config.IMAGE_MODEL
    if _refused_recently(model):
        raise _not_on_free_tier("image generation", model)
    client = _client()

    from google.genai import types

    contents: list = [prompt.strip()]
    if reference_image.strip():
        source = _resolve(reference_image)
        if not source.is_file():
            raise ToolError(
                f"no such image: {_relative(source)}",
                hint="check the path with list_directory",
            )
        suffix = source.suffix.lower().lstrip(".")
        contents.insert(0, types.Part.from_bytes(
            data=source.read_bytes(),
            mime_type=f"image/{'jpeg' if suffix in ('jpg', 'jpeg') else suffix}",
        ))

    started = time.time()
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"]
            ),
        )
    except Exception as exc:  # noqa: BLE001 - turned into a specific message
        raise _explain(exc, "image generation", model) from None

    data, said = None, []
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data and data is None:
                data = inline.data
            elif getattr(part, "text", None):
                said.append(part.text)

    if data is None:
        raise ToolError(
            "the model replied but sent no picture",
            hint=(f"it said: {' '.join(said)[:140]}" if said
                  else "try describing the image differently"),
        )

    target = _target(path, fmt)
    # The model returns PNG. Anything else is a conversion, and Pillow is
    # already a dependency for the rest of the media tools.
    if fmt in ("jpg", "jpeg", "webp"):
        import io

        from PIL import Image

        picture = Image.open(io.BytesIO(data))
        if fmt in ("jpg", "jpeg") and picture.mode in ("RGBA", "P"):
            picture = picture.convert("RGB")   # jpeg has no alpha channel
        picture.save(target)
    else:
        target.write_bytes(data)

    edited = _relative(_resolve(reference_image)) if reference_image.strip() else None
    return {
        "created": _relative(target),
        "format": fmt,
        "size_bytes": target.stat().st_size,
        "seconds": round(time.time() - started, 1),
        "model": model,
        "edited_from": edited,
        "note": " ".join(said)[:200] or None,
    }


@tool(group="imagery")
def generate_video(
    prompt: str, path: str, seconds: int = 8, aspect_ratio: str = "16:9"
) -> dict:
    """Generate a short video from a description, and save it.

    This takes minutes rather than seconds, and it blocks until the file
    exists -- returning early would have you report a video that is not there
    yet. Tell the user it will take a while before calling it.

    Args:
        prompt: What should happen in the video.
        path: Where to save it, e.g. "Desktop/clip".
        seconds: Roughly how long, in seconds.
        aspect_ratio: "16:9" for landscape, "9:16" for portrait.
    """
    if not prompt.strip():
        raise ToolError("no prompt given", hint="describe the video you want")

    fmt = "mp4"
    for candidate in VIDEO_FORMATS:
        if path.lower().endswith(f".{candidate}"):
            fmt = candidate
            break

    model = config.VIDEO_MODEL
    if _refused_recently(model):
        raise _not_on_free_tier("video generation", model)
    client = _client()
    started = time.time()

    try:
        from google.genai import types

        operation = client.models.generate_videos(
            model=model,
            prompt=prompt.strip(),
            config=types.GenerateVideosConfig(
                duration_seconds=max(2, min(int(seconds), 60)),
                aspect_ratio=aspect_ratio,
                number_of_videos=1,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise _explain(exc, "video generation", model) from None

    # Poll rather than sleep-and-hope: the job takes as long as it takes, and
    # a fixed wait would either give up early or idle after it had finished.
    while not operation.done:
        if time.time() - started > VIDEO_TIMEOUT_S:
            raise ToolError(
                f"the video was still rendering after {VIDEO_TIMEOUT_S // 60} minutes",
                hint="it may still finish; try a shorter clip or a simpler prompt",
            )
        time.sleep(VIDEO_POLL_S)
        try:
            operation = client.operations.get(operation)
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "video generation", model) from None

    response = getattr(operation, "response", None)
    videos = getattr(response, "generated_videos", None) or []
    if not videos:
        reasons = getattr(response, "rai_media_filtered_reasons", None) or []
        raise ToolError(
            "the job finished without producing a video",
            hint=(f"the model gave: {reasons[0][:140]}" if reasons
                  else "try describing it differently"),
        )

    video = videos[0].video
    data = getattr(video, "video_bytes", None)
    if not data:
        try:
            client.files.download(file=video)
            data = getattr(video, "video_bytes", None)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(
                f"the video generated but could not be downloaded ({type(exc).__name__})",
                hint="the link may have expired; generating it again is the fix",
            ) from None

    if not data:
        raise ToolError(
            "the video came back empty",
            hint="generate it again; if it repeats, the model is at fault",
        )

    target = _target(path, fmt)
    target.write_bytes(data)

    return {
        "created": _relative(target),
        "format": fmt,
        "size_bytes": target.stat().st_size,
        "seconds_taken": round(time.time() - started, 1),
        "model": model,
        "note": "convert_media will change the container if a different one is wanted",
    }
