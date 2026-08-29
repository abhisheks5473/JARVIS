"""Audio, video and images: MP3, MP4, WAV, GIF, PNG and conversions between.

No ffmpeg binary to install. PyAV is already here for the speech stack and
carries its own libav, so mp3, aac, h264 and gif encoding come free -- which
matters most for the shipped .exe, where asking a recipient to install ffmpeg
and put it on PATH would end the conversation.

Speech comes from the Piper voice already used for talking aloud, so "read
this out and save it as an mp3" costs no API quota at all.

Five tools rather than a dozen, each taking an enum, for the same reason the
document tool does: capability should not be bought with selection accuracy.
"""
from __future__ import annotations

import wave
from pathlib import Path

from .base import ToolError, tool
from .files import _relative, _resolve

AUDIO_FORMATS = ("mp3", "wav", "m4a", "aac", "flac", "ogg")
VIDEO_FORMATS = ("mp4", "gif", "webm", "avi", "mkv")
IMAGE_FORMATS = ("png", "jpg", "jpeg", "webp", "bmp", "gif")

_CODEC_FOR = {
    "mp3": "libmp3lame", "m4a": "aac", "aac": "aac",
    "flac": "flac", "ogg": "libvorbis", "wav": "pcm_s16le",
    "mp4": "libx264", "webm": "libvpx", "avi": "mpeg4", "mkv": "libx264",
}


def _av():
    try:
        import av
    except ImportError:
        raise ToolError(
            "PyAV is not installed, so audio and video are unavailable",
            hint="run: pip install av",
        ) from None
    return av


def _target(path: str, fmt: str) -> Path:
    candidate = path if path.lower().endswith(f".{fmt}") else f"{path}.{fmt}"
    target = _resolve(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _encode_audio(samples, sample_rate: int, target: Path, fmt: str) -> None:
    """Write mono int16 samples out in whatever container was asked for."""
    av = _av()
    container = av.open(str(target), mode="w")
    try:
        stream = container.add_stream(
            _CODEC_FOR.get(fmt, "libmp3lame"), rate=sample_rate
        )
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = sample_rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):  # flush, or the tail is truncated
            container.mux(packet)
    finally:
        container.close()


@tool(group="media")
def create_audio(text: str, path: str, format: str = "mp3") -> dict:
    """Turn text into a spoken audio file.

    Use for "read this out and save it", an audio note, or a narration track.
    The voice is the same offline one used for speaking aloud, so this costs
    no API quota and works with no internet.

    Args:
        text: What should be spoken.
        path: Where to save it, e.g. "Desktop/summary". Extension optional.
        format: mp3, wav, m4a, aac, flac or ogg.
    """
    fmt = format.lower().lstrip(".")
    if fmt not in AUDIO_FORMATS:
        raise ToolError(
            f"unknown audio format: {format}",
            hint=f"use one of {', '.join(AUDIO_FORMATS)}",
        )
    if not text.strip():
        raise ToolError("nothing to say", hint="give some text to speak")
    if len(text) > 20000:
        raise ToolError(
            "that is a lot of speech for one file", hint="split it into several files"
        )

    from ..voice.tts import _clean_for_speech, speaker

    if not speaker.load():
        raise ToolError(
            f"the speech voice is not available ({speaker.last_error})",
            hint="it downloads on first use, so this needs internet once",
        )

    import numpy as np

    chunks = [
        c.audio_int16_array.reshape(-1)
        for c in speaker._voice.synthesize(_clean_for_speech(text))
    ]
    if not chunks:
        raise ToolError("the voice produced no audio", hint="try different text")

    samples = np.concatenate(chunks)
    rate = speaker._sample_rate
    target = _target(path, fmt)

    if fmt == "wav":
        # A plain WAV needs no encoder and stays exact.
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(samples.tobytes())
    else:
        _encode_audio(samples, rate, target, fmt)

    return {
        "created": _relative(target),
        "format": fmt,
        "seconds": round(len(samples) / rate, 1),
        "size_bytes": target.stat().st_size,
        "full_path": str(target),
    }


@tool(group="media")
def create_video(
    images: list[str], path: str, seconds_each: float = 2.5, format: str = "mp4"
) -> dict:
    """Build a video or animated GIF from a list of image files.

    Use for a slideshow, a before-and-after, or turning screenshots into
    something shareable. Images are letterboxed onto a common canvas, so they
    do not all need to be the same size.

    Args:
        images: Paths to the images, in the order they should appear.
        path: Where to save it, e.g. "Desktop/slideshow".
        seconds_each: How long each image is held on screen.
        format: mp4, gif, webm, avi or mkv.
    """
    fmt = format.lower().lstrip(".")
    if fmt not in VIDEO_FORMATS:
        raise ToolError(
            f"unknown video format: {format}",
            hint=f"use one of {', '.join(VIDEO_FORMATS)}",
        )
    if not images:
        raise ToolError("no images given", hint="pass a list of image paths")

    from PIL import Image

    resolved = []
    for item in images[:200]:
        candidate = _resolve(item)
        if not candidate.is_file():
            raise ToolError(
                f"no such image: {_relative(candidate)}",
                hint="check the paths with list_directory",
            )
        resolved.append(candidate)

    # One canvas for every frame: encoders reject a stream whose dimensions
    # change part-way through, and h264 additionally requires even numbers.
    widths, heights = [], []
    for item in resolved:
        with Image.open(item) as probe:
            widths.append(probe.width)
            heights.append(probe.height)
    width = max(2, min(max(widths), 1920)) // 2 * 2
    height = max(2, min(max(heights), 1080)) // 2 * 2

    hold = max(0.2, min(float(seconds_each), 30.0))
    fps = 24
    frames_each = max(1, int(round(hold * fps)))
    target = _target(path, fmt)

    av = _av()
    container = av.open(str(target), mode="w")
    try:
        stream = container.add_stream(
            "gif" if fmt == "gif" else _CODEC_FOR.get(fmt, "libx264"), rate=fps
        )
        stream.width, stream.height = width, height
        stream.pix_fmt = "rgb8" if fmt == "gif" else "yuv420p"

        for item in resolved:
            with Image.open(item) as source:
                picture = source.convert("RGB")
                picture.thumbnail((width, height), Image.LANCZOS)
                canvas = Image.new("RGB", (width, height), (0, 0, 0))
                canvas.paste(
                    picture,
                    ((width - picture.width) // 2, (height - picture.height) // 2),
                )
            frame = av.VideoFrame.from_image(canvas)
            for _ in range(frames_each):
                for packet in stream.encode(frame):
                    container.mux(packet)

        for packet in stream.encode(None):
            container.mux(packet)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            f"could not build the video: {type(exc).__name__}: {exc}",
            hint="try format='gif', or fewer images",
        ) from None
    finally:
        container.close()

    return {
        "created": _relative(target),
        "format": fmt,
        "images": len(resolved),
        "seconds": round(len(resolved) * hold, 1),
        "resolution": f"{width}x{height}",
        "size_bytes": target.stat().st_size,
        "full_path": str(target),
    }


@tool(group="media")
def convert_media(source: str, destination: str) -> dict:
    """Convert audio or video from one format to another.

    Handles mp3, wav, m4a, aac, flac, ogg, mp4, webm, avi, mkv and gif, and
    extracts the audio from a video when the destination is an audio format.
    The format comes from the destination extension.

    Args:
        source: The file to convert.
        destination: Where to write it, with the extension you want.
    """
    inp = _resolve(source)
    if not inp.is_file():
        raise ToolError(
            f"no such file: {_relative(inp)}", hint="check with list_directory"
        )

    fmt = Path(destination).suffix.lower().lstrip(".")
    if fmt not in AUDIO_FORMATS + VIDEO_FORMATS:
        raise ToolError(
            f"cannot convert to {fmt or 'a file with no extension'}",
            hint=f"use one of {', '.join(AUDIO_FORMATS + VIDEO_FORMATS)}",
        )

    target = _target(destination, fmt)
    audio_only = fmt in AUDIO_FORMATS
    av = _av()

    try:
        with av.open(str(inp)) as source_container:
            out = av.open(str(target), mode="w")
            try:
                in_audio = (
                    source_container.streams.audio[0]
                    if source_container.streams.audio
                    else None
                )
                in_video = (
                    source_container.streams.video[0]
                    if source_container.streams.video and not audio_only
                    else None
                )
                if in_audio is None and audio_only:
                    raise ToolError(
                        f"{_relative(inp)} has no audio track to extract",
                        hint=(
                            "it is a silent video; convert it to a video "
                            "format instead, or check the source file"
                        ),
                    )
                if in_audio is None and in_video is None:
                    raise ToolError(
                        "that file has no audio or video to convert",
                        hint="check it is a real media file",
                    )

                out_audio = out_video = None
                if in_audio is not None:
                    out_audio = out.add_stream(
                        _CODEC_FOR.get(fmt, "libmp3lame") if audio_only else "aac",
                        rate=in_audio.rate,
                    )
                if in_video is not None:
                    out_video = out.add_stream(_CODEC_FOR.get(fmt, "libx264"))
                    out_video.width = in_video.codec_context.width // 2 * 2
                    out_video.height = in_video.codec_context.height // 2 * 2
                    out_video.pix_fmt = "yuv420p"

                streams = [s for s in (in_audio, in_video) if s is not None]
                for frame in source_container.decode(*streams):
                    if isinstance(frame, av.AudioFrame) and out_audio is not None:
                        frame.pts = None
                        for packet in out_audio.encode(frame):
                            out.mux(packet)
                    elif isinstance(frame, av.VideoFrame) and out_video is not None:
                        frame.pts = None
                        for packet in out_video.encode(frame):
                            out.mux(packet)

                for stream in (out_audio, out_video):
                    if stream is not None:
                        for packet in stream.encode(None):
                            out.mux(packet)
            finally:
                out.close()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            f"could not convert that file: {type(exc).__name__}: {exc}",
            hint="the source may be corrupt or in an unsupported codec",
        ) from None

    return {
        "created": _relative(target),
        "format": fmt,
        "from": _relative(inp),
        "audio_only": audio_only,
        "size_bytes": target.stat().st_size,
        "full_path": str(target),
    }


@tool(group="media")
def edit_image(
    source: str,
    destination: str,
    width: int = 0,
    height: int = 0,
    rotate: int = 0,
    grayscale: bool = False,
) -> dict:
    """Resize, convert, rotate or desaturate an image.

    The output format comes from the destination extension, so this doubles as
    a converter. Giving only a width keeps the aspect ratio.

    Args:
        source: The image to read.
        destination: Where to write it, e.g. "Desktop/small.jpg".
        width: Target width in pixels. 0 leaves it alone.
        height: Target height in pixels. 0 keeps the aspect ratio.
        rotate: Degrees anticlockwise: 0, 90, 180 or 270.
        grayscale: Convert to black and white.
    """
    from PIL import Image

    inp = _resolve(source)
    if not inp.is_file():
        raise ToolError(f"no such image: {_relative(inp)}", hint="check the path")

    fmt = Path(destination).suffix.lower().lstrip(".")
    if fmt not in IMAGE_FORMATS:
        raise ToolError(
            f"cannot write {fmt or 'a file with no extension'}",
            hint=f"use one of {', '.join(IMAGE_FORMATS)}",
        )
    if rotate not in (0, 90, 180, 270):
        raise ToolError("rotate must be 0, 90, 180 or 270", hint="use a right angle")

    target = _target(destination, fmt)
    try:
        with Image.open(inp) as picture:
            original = f"{picture.width}x{picture.height}"
            if grayscale:
                picture = picture.convert("L")
            if rotate:
                picture = picture.rotate(rotate, expand=True)
            if width or height:
                new_w = width or int(picture.width * (height / picture.height))
                new_h = height or int(picture.height * (width / picture.width))
                picture = picture.resize((max(1, new_w), max(1, new_h)), Image.LANCZOS)
            # JPEG has no alpha channel; converting avoids a confusing failure.
            if fmt in ("jpg", "jpeg") and picture.mode in ("RGBA", "P", "LA"):
                picture = picture.convert("RGB")
            picture.save(target)
            final = f"{picture.width}x{picture.height}"
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            f"could not edit that image: {type(exc).__name__}",
            hint="it may be corrupt or not an image",
        ) from None

    return {
        "created": _relative(target),
        "from_size": original,
        "to_size": final,
        "format": fmt,
        "size_bytes": target.stat().st_size,
        "full_path": str(target),
    }


@tool(group="media")
def media_info(path: str) -> dict:
    """Report what is inside an audio, video or image file.

    Duration, resolution, codecs and sample rate. Call this before converting
    something, so the answer describes the real file rather than a guess.

    Args:
        path: The media file to inspect.
    """
    target = _resolve(path)
    if not target.is_file():
        raise ToolError(f"no such file: {_relative(target)}", hint="check the path")

    if target.suffix.lower().lstrip(".") in IMAGE_FORMATS:
        from PIL import Image

        with Image.open(target) as picture:
            return {
                "path": _relative(target),
                "kind": "image",
                "resolution": f"{picture.width}x{picture.height}",
                "mode": picture.mode,
                "size_bytes": target.stat().st_size,
            }

    av = _av()
    try:
        with av.open(str(target)) as container:
            info: dict = {
                "path": _relative(target),
                "kind": "video" if container.streams.video else "audio",
                "seconds": round((container.duration or 0) / 1_000_000, 1),
                "size_bytes": target.stat().st_size,
            }
            if container.streams.video:
                video = container.streams.video[0]
                info["resolution"] = (
                    f"{video.codec_context.width}x{video.codec_context.height}"
                )
                info["video_codec"] = video.codec_context.name
            if container.streams.audio:
                audio = container.streams.audio[0]
                info["audio_codec"] = audio.codec_context.name
                info["sample_rate"] = audio.rate
            return info
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            f"could not read that media file: {type(exc).__name__}",
            hint="it may be corrupt or not a media file",
        ) from None
