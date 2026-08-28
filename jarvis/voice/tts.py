"""Mouth: Piper, local and free.

Local TTS does not touch your Gemini quota, which you need for reasoning. It
is also faster than a cloud round trip and works with no network at all.

Two things make the difference between "has a voice" and "feels responsive":

  * **Speak sentence by sentence.** A voice round trip is roughly 0.5s of
    speech-to-text, 1-3s of model, 0.5s of synthesis, plus a call per tool.
    Anything past about three seconds feels broken to someone talking out
    loud. Starting to speak the first sentence while the rest is still
    generating buys more perceived speed than any model swap.

  * **Barge-in.** Let the user cut you off. An assistant you cannot interrupt
    is infuriating in a way that is hard to appreciate until you have built
    one. `stop()` kills playback immediately, and `is_speaking` lets the
    microphone mute itself so it does not transcribe its own voice and
    cheerfully reply to itself.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Iterable

from .. import config

# Split on sentence ends, but not on decimals or mid-sentence punctuation.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def _clean_for_speech(text: str) -> str:
    """Strip anything that sounds absurd read aloud.

    The system prompt already asks for plain prose, but models slip markdown
    in occasionally, and a synthesiser reading asterisks aloud ruins the
    effect instantly.
    """
    cleaned = re.sub(r"```.*?```", " code omitted ", text, flags=re.S)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)  # links
    cleaned = re.sub(r"https?://\S+", " a link ", cleaned)
    cleaned = re.sub(r"^\s*[-•]\s*", "", cleaned, flags=re.M)
    cleaned = re.sub(r"[*_#>|]+", " ", cleaned)
    return " ".join(cleaned.split())


class Speaker:
    """Piper voice with interruptible playback."""

    def __init__(self, voice_name: str | None = None) -> None:
        self.voice_name = voice_name or config.VOICE.piper_voice
        self._voice = None
        self._sample_rate = 22050
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.is_speaking = False
        self.last_error = ""

    # ------------------------------------------------------------ loading
    def _voice_dir(self) -> Path:
        path = config.VOICE_DIR / "piper"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load(self) -> bool:
        """Load the voice, downloading it once if it is not there yet."""
        if self._voice is not None:
            return True
        try:
            from piper import PiperVoice
            from piper.download_voices import download_voice
        except ImportError as exc:
            self.last_error = f"piper-tts is not installed ({exc})"
            return False

        directory = self._voice_dir()
        model = directory / f"{self.voice_name}.onnx"

        if not model.exists():
            try:
                download_voice(self.voice_name, directory)
            except Exception as exc:  # noqa: BLE001 - network, bad name, disk
                self.last_error = f"could not download voice {self.voice_name}: {exc}"
                return False

        try:
            self._voice = PiperVoice.load(str(model))
            self._sample_rate = int(
                getattr(getattr(self._voice, "config", None), "sample_rate", 22050)
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"could not load voice: {exc}"
            return False
        return True

    # ------------------------------------------------------------ speaking
    def stop(self) -> None:
        """Interrupt playback immediately. This is barge-in."""
        self._stop.set()

    def speak(self, text: str, blocking: bool = True) -> bool:
        """Say one piece of text. Returns False if interrupted or failed."""
        cleaned = _clean_for_speech(text)
        if not cleaned:
            return True
        if not self.load():
            return False

        if blocking:
            return self._render(cleaned)

        threading.Thread(target=self._render, args=(cleaned,), daemon=True).start()
        return True

    def _render(self, text: str) -> bool:
        try:
            import sounddevice as sd
        except ImportError:
            self.last_error = "sounddevice is not installed"
            return False

        with self._lock:
            self._stop.clear()
            self.is_speaking = True
            completed = True
            try:
                stream = sd.RawOutputStream(
                    samplerate=self._sample_rate, channels=1, dtype="int16"
                )
                stream.start()
                try:
                    for chunk in self._voice.synthesize(text):
                        if self._stop.is_set():
                            completed = False
                            break
                        stream.write(chunk.audio_int16_bytes)
                finally:
                    stream.stop()
                    stream.close()
            except Exception as exc:  # noqa: BLE001 - audio devices are flaky
                self.last_error = f"playback failed: {exc}"
                completed = False
            finally:
                self.is_speaking = False
            return completed

    def speak_stream(self, chunks: Iterable[str]) -> str:
        """Speak text as it arrives, one sentence at a time.

        `chunks` is any iterable of partial strings -- typically the text
        deltas from a streaming interaction. Returns everything spoken, so the
        caller still gets the full reply for logging.
        """
        buffer = ""
        spoken: list[str] = []

        for chunk in chunks:
            if self._stop.is_set():
                break
            buffer += chunk
            # Speak whole sentences as soon as they are complete.
            while True:
                parts = _SENTENCE_END.split(buffer, maxsplit=1)
                if len(parts) < 2:
                    break
                sentence, buffer = parts
                spoken.append(sentence)
                self.speak(sentence)

        if buffer.strip() and not self._stop.is_set():
            spoken.append(buffer)
            self.speak(buffer)

        return " ".join(spoken)


speaker = Speaker()
