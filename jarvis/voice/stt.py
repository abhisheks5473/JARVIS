"""Ears: faster-whisper, local and free.

Local speech-to-text costs no Gemini quota, works offline, and keeps your
voice off someone else's servers. `base.en` runs comfortably on a laptop CPU;
`small.en` is better if you have a GPU or patience.

Recording stops on silence rather than after a fixed window, so short commands
feel instant and long ones are not cut off. The voice-activity detection is
deliberately a plain energy threshold: it is a few lines, it has no model to
load, and it is calibrated against the actual room at startup rather than
against a constant somebody picked on a different machine.
"""
from __future__ import annotations

import threading
import time

from .. import config


class Ears:
    """Microphone capture plus transcription."""

    def __init__(self) -> None:
        self._model = None
        self._noise_floor: float | None = None
        self._stop = threading.Event()
        self.last_error = ""
        self.is_listening = False
        # Loudness of the last block, 0..1ish. The window draws
        # from this, so the orb reacts to the actual voice
        # rather than to a timer pretending to be one.
        self.level = 0.0

    # ------------------------------------------------------------ loading
    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            self.last_error = f"faster-whisper is not installed ({exc})"
            return False

        try:
            self._model = WhisperModel(
                config.VOICE.stt_model,
                device=config.VOICE.stt_device,
                compute_type=config.VOICE.stt_compute,
                download_root=str(config.VOICE_DIR / "whisper"),
            )
        except Exception as exc:  # noqa: BLE001 - model download or bad device
            self.last_error = f"could not load the speech model: {exc}"
            return False
        return True

    # ------------------------------------------------------------ calibrate
    def calibrate(self, seconds: float = 0.6) -> float:
        """Measure the room's noise floor.

        A fixed threshold works on the machine it was tuned on and nowhere
        else. Half a second of ambient audio at startup is enough to make the
        silence detection behave the same in a quiet room and a noisy one.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            self.last_error = str(exc)
            return 0.01

        try:
            samples = sd.rec(
                int(seconds * config.VOICE.sample_rate),
                samplerate=config.VOICE.sample_rate,
                channels=1,
                dtype="float32",
            )
            sd.wait()
            self._noise_floor = float(np.sqrt(np.mean(samples**2)))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"microphone unavailable: {exc}"
            self._noise_floor = 0.01
        return self._noise_floor or 0.01

    def _threshold(self) -> float:
        floor = self._noise_floor if self._noise_floor is not None else 0.01
        # Three times the noise floor, with a sane minimum so a very quiet
        # room does not make the threshold effectively zero.
        return max(floor * 3.0, 0.012)

    # ------------------------------------------------------------ record
    def stop(self) -> None:
        self._stop.set()

    def record(self, max_seconds: float | None = None):
        """Record until the speaker stops, and return float32 mono audio.

        Returns None if nothing audible was captured, which the caller should
        treat as "they changed their mind", not as an error.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            self.last_error = str(exc)
            return None

        if self._noise_floor is None:
            self.calibrate()

        rate = config.VOICE.sample_rate
        block = int(rate * 0.05)  # 50ms blocks: responsive without thrashing
        limit = max_seconds or config.VOICE.max_utterance_s
        threshold = self._threshold()

        collected: list = []
        heard_speech = False
        silent_for = 0.0
        started = time.time()

        self._stop.clear()
        self.is_listening = True

        try:
            with sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=block
            ) as stream:
                while not self._stop.is_set():
                    if time.time() - started > limit:
                        break

                    chunk, overflowed = stream.read(block)
                    if overflowed:
                        continue

                    collected.append(chunk.copy())
                    level = float(np.sqrt(np.mean(chunk**2)))
                    self.level = level

                    if level > threshold:
                        heard_speech = True
                        silent_for = 0.0
                    elif heard_speech:
                        silent_for += 0.05
                        if silent_for >= config.VOICE.silence_timeout:
                            break
                    elif time.time() - started > 3.0:
                        # Nothing said at all: give up rather than hang.
                        break
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"recording failed: {exc}"
            return None
        finally:
            self.is_listening = False
            self.level = 0.0

        if not heard_speech or not collected:
            return None
        return np.concatenate(collected, axis=0).flatten()

    # ------------------------------------------------------------ transcribe
    def transcribe(self, audio) -> str:
        if audio is None or not self.load():
            return ""
        try:
            segments, _info = self._model.transcribe(
                audio,
                language="en",
                beam_size=1,          # greedy: noticeably faster, barely worse
                vad_filter=True,      # drop leading and trailing silence
                condition_on_previous_text=False,
            )
            return " ".join(segment.text for segment in segments).strip()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"transcription failed: {exc}"
            return ""

    def listen(self, max_seconds: float | None = None) -> str:
        """Record and transcribe in one call."""
        return self.transcribe(self.record(max_seconds))


ears = Ears()
