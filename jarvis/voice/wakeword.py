"""A wake word you choose, recorded in your own voice.

Nothing here is trained on anybody else's speech, and that is the point. A
shipped wake word is one fixed phrase learned from thousands of speakers, and
it works best on the accents that dominated that data. This learns *your*
phrase from *your* voice, from five examples, in about twenty seconds -- and
it is speaker-dependent on purpose: it should answer to you saying it, not to
the television.

**How it works.** Each recording becomes a sequence of MFCC vectors -- a
compact description of the shape of the sound, with loudness and microphone
colour normalised away. Two utterances of the same phrase are never the same
length, so they are compared with dynamic time warping, which stretches one
against the other to find the best alignment and reports how far apart they
are. A candidate is a wake word if it lands close to the recordings you gave.

**Why not a neural model.** Few-shot keyword spotting from five samples is
what DTW is genuinely good at. A trained detector needs far more data than
anyone will record by hand, and the pretrained ones only accept the phrases
they already know. This runs in a few milliseconds of pure numpy, needs no
download, and never sends audio anywhere -- the microphone stays on this
machine.

**The threshold is measured, not guessed.** After enrolment the recordings are
compared against each other. How much *you* vary between repetitions is the
natural scale for how close an utterance has to be, so the threshold comes
from that spread rather than from a constant that happened to suit one laptop.
"""
from __future__ import annotations

import threading
import time

from .. import config

# 25ms window, 10ms hop: the standard speech framing, and short enough that a
# one-second phrase still yields ~100 frames to align.
FRAME_MS = 25
HOP_MS = 10
N_MELS = 26
N_CEPS = 13          # keep 1..12; c0 is loudness, which we deliberately drop
N_FFT = 512

TEMPLATE_FILE = config.VOICE_DIR / "wakeword.npz"

# A wake phrase shorter than this is a cough; longer than this is a sentence.
MIN_PHRASE_S = 0.25
MAX_PHRASE_S = 3.0

REQUIRED_SAMPLES = 5

_CACHE: dict = {}


# ------------------------------------------------------------------ features
def _hz_to_mel(hz):
    import numpy as np

    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel):
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(rate: int):
    """Triangular mel filters, built once per sample rate."""
    import numpy as np

    key = ("mel", rate)
    if key in _CACHE:
        return _CACHE[key]

    low, high = 300.0, min(8000.0, rate / 2.0)
    points = _mel_to_hz(np.linspace(_hz_to_mel(low), _hz_to_mel(high), N_MELS + 2))
    bins = np.clip(np.floor((N_FFT + 1) * points / rate).astype(int), 0, N_FFT // 2)

    filters = np.zeros((N_MELS, N_FFT // 2 + 1))
    for m in range(N_MELS):
        left, centre, right = int(bins[m]), int(bins[m + 1]), int(bins[m + 2])
        centre = max(centre, left + 1)
        right = max(right, centre + 1)
        if right > N_FFT // 2:
            break
        filters[m, left:centre] = (np.arange(left, centre) - left) / (centre - left)
        filters[m, centre:right] = (right - np.arange(centre, right)) / (right - centre)

    _CACHE[key] = filters
    return filters


def _dct_matrix():
    """DCT-II, which decorrelates the log-mel energies into cepstra."""
    import numpy as np

    if "dct" in _CACHE:
        return _CACHE["dct"]
    k = np.arange(N_CEPS)[:, None]
    n = np.arange(N_MELS)[None, :]
    _CACHE["dct"] = np.cos(np.pi * k * (2 * n + 1) / (2 * N_MELS))
    return _CACHE["dct"]


def features(audio, rate: int | None = None):
    """Turn mono float32 audio into normalised MFCC frames, or None.

    Rows are L2-normalised so DTW can use a cosine distance, and the whole
    sequence is mean/variance normalised over time so that a different
    microphone or a quieter room does not move the entire pattern.
    """
    import numpy as np

    rate = rate or config.VOICE.sample_rate
    audio = np.asarray(audio, dtype=np.float64).flatten()

    frame_len = int(rate * FRAME_MS / 1000)
    hop = int(rate * HOP_MS / 1000)
    if audio.size < frame_len * 2:
        return None

    # Pre-emphasis lifts the high frequencies, where the consonants live.
    audio = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])

    count = 1 + (audio.size - frame_len) // hop
    index = np.arange(frame_len)[None, :] + hop * np.arange(count)[:, None]
    frames = audio[index] * np.hamming(frame_len)[None, :]

    power = (np.abs(np.fft.rfft(frames, N_FFT)) ** 2) / N_FFT
    energy = _mel_filterbank(rate) @ power.T
    cepstra = (_dct_matrix() @ np.log(np.maximum(energy, 1e-10))).T[:, 1:N_CEPS]

    # Cepstral mean and variance normalisation removes the constant colour of
    # the microphone and the room, which would otherwise dominate the distance.
    cepstra = cepstra - cepstra.mean(axis=0, keepdims=True)
    cepstra = cepstra / np.maximum(cepstra.std(axis=0, keepdims=True), 1e-8)

    norms = np.linalg.norm(cepstra, axis=1, keepdims=True)
    return cepstra / np.maximum(norms, 1e-8)


def trim_silence(audio, rate: int | None = None, pad_ms: int = 60):
    """Cut leading and trailing silence so the templates align on speech."""
    import numpy as np

    rate = rate or config.VOICE.sample_rate
    audio = np.asarray(audio, dtype=np.float32).flatten()
    if audio.size == 0:
        return audio

    block = max(1, int(rate * 0.01))
    usable = (audio.size // block) * block
    if usable < block:
        return audio
    levels = np.sqrt((audio[:usable].reshape(-1, block) ** 2).mean(axis=1))

    # Relative to this clip's own peak, so it works at any recording volume.
    loud = levels > max(levels.max() * 0.12, 1e-4)
    if not loud.any():
        return audio

    first = int(np.argmax(loud))
    last = int(len(loud) - np.argmax(loud[::-1]))
    pad = int(pad_ms / 10)
    return audio[max(0, first - pad) * block : min(len(loud), last + pad) * block]


# ----------------------------------------------------------------- alignment
def dtw_distance(a, b, band: float = 0.25) -> float:
    """Distance between two MFCC sequences, aligned by time warping.

    The Sakoe-Chiba band stops the alignment wandering far from the diagonal,
    which both speeds it up and rejects matches that would only line up by
    stretching one utterance past recognition.
    """
    import numpy as np

    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return float("inf")

    cost = 1.0 - a @ b.T                # cosine distance; rows are unit norm
    n, m = cost.shape
    width = max(int(band * max(n, m)), abs(n - m) + 1)

    acc = np.full((n + 1, m + 1), np.inf)
    acc[0, 0] = 0.0
    for i in range(1, n + 1):
        low, high = max(1, i - width), min(m, i + width)
        row, prev, line = acc[i], acc[i - 1], cost[i - 1]
        for j in range(low, high + 1):
            row[j] = line[j - 1] + min(prev[j], row[j - 1], prev[j - 1])

    total = acc[n, m]
    if not np.isfinite(total):
        return float("inf")
    return float(total / (n + m))       # per-step, so length does not bias it


class WakeWord:
    """Enrolment, matching, and the always-on listener."""

    COOLDOWN_S = 2.0        # ignore repeat triggers from one utterance
    END_SILENCE_S = 0.35    # silence that ends a candidate utterance

    def __init__(self) -> None:
        self.phrase = ""
        self.templates: list = []
        self.threshold = 0.0
        self.spread = (0.0, 0.0)
        self.last_error = ""
        self.last_score: float | None = None
        self.listening = False
        self._thread = None
        self._stop = threading.Event()
        self._paused = threading.Event()

    # ----------------------------------------------------------- persistence
    @property
    def trained(self) -> bool:
        return len(self.templates) > 0

    def load(self) -> bool:
        import numpy as np

        if not TEMPLATE_FILE.exists():
            return False
        try:
            data = np.load(TEMPLATE_FILE, allow_pickle=False)
            self.templates = [data[f"t{i}"] for i in range(int(data["count"]))]
            self.threshold = float(data["threshold"])
            self.phrase = str(data["phrase"])
            self.spread = (float(data["dur_min"]), float(data["dur_max"]))
        except Exception as exc:  # noqa: BLE001 - a corrupt file must not crash
            self.last_error = f"could not read the wake word: {exc}"
            self.templates = []
            return False
        return True

    def save(self) -> None:
        import numpy as np

        TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            TEMPLATE_FILE,
            count=len(self.templates),
            threshold=self.threshold,
            phrase=self.phrase,
            dur_min=self.spread[0],
            dur_max=self.spread[1],
            **{f"t{i}": t for i, t in enumerate(self.templates)},
        )

    def forget(self) -> None:
        self.templates, self.threshold, self.phrase = [], 0.0, ""
        TEMPLATE_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------- enrolment
    def enroll_from_audio(self, clips: list, phrase: str = "") -> dict:
        """Build the templates from recorded clips, and report what happened.

        Separate from recording so it can be tested with audio from a file
        rather than only from a live microphone.
        """
        import numpy as np

        rate = config.VOICE.sample_rate
        templates, durations, rejected = [], [], []

        for number, clip in enumerate(clips, 1):
            trimmed = trim_silence(clip, rate)
            seconds = len(trimmed) / rate
            if seconds < MIN_PHRASE_S:
                rejected.append(f"recording {number} was too short ({seconds:.2f}s)")
                continue
            if seconds > MAX_PHRASE_S:
                rejected.append(f"recording {number} was too long ({seconds:.1f}s)")
                continue
            mfcc = features(trimmed, rate)
            if mfcc is None:
                rejected.append(f"recording {number} had nothing audible in it")
                continue
            templates.append(mfcc)
            durations.append(seconds)

        if len(templates) < 3:
            return {
                "ok": False,
                "kept": len(templates),
                "problems": rejected,
                "message": "too few usable recordings; say the phrase clearly and retry",
            }

        # How much the speaker varies between their own repetitions is the
        # natural scale for "close enough".
        pairs = [
            dtw_distance(templates[i], templates[j])
            for i in range(len(templates))
            for j in range(i + 1, len(templates))
        ]
        pairs = [p for p in pairs if np.isfinite(p)]
        if not pairs:
            return {"ok": False, "message": "the recordings could not be compared"}

        mean, deviation = float(np.mean(pairs)), float(np.std(pairs))
        sensitivity = float(config.VOICE.wake_sensitivity)
        base = min(mean + 1.2 * deviation, max(pairs) * 1.15)
        self.threshold = float(base * (0.75 + 0.5 * sensitivity))
        self.templates = templates
        self.phrase = phrase.strip()
        self.spread = (min(durations), max(durations))
        self.save()

        return {
            "ok": True,
            "kept": len(templates),
            "problems": rejected,
            "threshold": round(self.threshold, 4),
            "agreement": round(mean, 4),
            "consistent": mean < self.threshold,
            "message": (
                f"learned from {len(templates)} recordings"
                + (f"; {len(rejected)} discarded" if rejected else "")
            ),
        }

    def record_sample(self, max_seconds: float = 3.0, settle: float = 0.25):
        """Record one spoken phrase from the microphone, or return None."""
        import numpy as np
        import sounddevice as sd

        rate = config.VOICE.sample_rate
        block = int(rate * 0.02)

        try:
            with sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=block
            ) as stream:
                # Measure the room first, so the same code works in a quiet
                # study and a noisy kitchen.
                floor = []
                for _ in range(15):
                    chunk, _over = stream.read(block)
                    floor.append(float(np.sqrt(np.mean(chunk**2))))
                threshold = max(float(np.median(floor)) * 3.5, 0.012)

                collected: list = []
                started, quiet = False, 0.0
                began = time.time()
                while time.time() - began < max_seconds + 2.0:
                    chunk, _over = stream.read(block)
                    level = float(np.sqrt(np.mean(chunk**2)))
                    if level > threshold:
                        started, quiet = True, 0.0
                        collected.append(chunk.copy())
                    elif started:
                        quiet += 0.02
                        collected.append(chunk.copy())
                        if quiet >= settle:
                            break
                    if started and len(collected) * 0.02 > max_seconds:
                        break
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"microphone unavailable: {exc}"
            return None

        if not collected:
            return None
        return np.concatenate(collected, axis=0).flatten()

    # -------------------------------------------------------------- matching
    def score(self, audio) -> float:
        """How far a clip is from the enrolled phrase. Lower is closer."""
        import numpy as np

        if not self.trained:
            return float("inf")
        rate = config.VOICE.sample_rate
        trimmed = trim_silence(audio, rate)
        seconds = len(trimmed) / rate

        # A candidate of obviously the wrong length is rejected before any
        # alignment happens: it is faster, and it stops DTW stretching
        # something unrelated into a shape that scores well.
        low, high = self.spread
        if seconds < low * 0.5 or seconds > high * 2.0:
            return float("inf")

        mfcc = features(trimmed, rate)
        if mfcc is None:
            return float("inf")

        distances = sorted(dtw_distance(mfcc, t) for t in self.templates)
        usable = [d for d in distances if np.isfinite(d)]
        if not usable:
            return float("inf")
        # Average of the three closest templates, so one unlucky recording
        # during enrolment cannot veto every future match.
        return float(np.mean(usable[: min(3, len(usable))]))

    def matches(self, audio) -> bool:
        self.last_score = self.score(audio)
        return self.last_score <= self.threshold

    # ------------------------------------------------------------- listening
    def start(self, on_wake, is_busy=None) -> bool:
        """Listen continuously, calling on_wake() when the phrase is heard.

        on_wake runs on the listener thread, so a GUI caller must marshal it.
        is_busy() is consulted before matching, so JARVIS does not wake itself
        while it is speaking.
        """
        if not self.trained and not self.load():
            self.last_error = "no wake word has been recorded yet"
            return False
        if self._thread and self._thread.is_alive():
            return True

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(on_wake, is_busy), daemon=True, name="wakeword"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.listening = False

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def _loop(self, on_wake, is_busy) -> None:
        import numpy as np
        import sounddevice as sd

        rate = config.VOICE.sample_rate
        block = int(rate * 0.02)
        max_samples = int(MAX_PHRASE_S * rate)

        try:
            with sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=block
            ) as stream:
                self.listening = True
                floor, calibrated = 0.01, False
                collected: list = []
                speaking, quiet, last_fire = False, 0.0, 0.0

                while not self._stop.is_set():
                    chunk, _over = stream.read(block)
                    level = float(np.sqrt(np.mean(chunk**2)))

                    if not calibrated:
                        floor, calibrated = max(level, 1e-4), True
                    elif not speaking:
                        # Track the room slowly, so a fan starting up does not
                        # leave the detector deaf for the rest of the session.
                        floor = 0.995 * floor + 0.005 * level

                    if self._paused.is_set() or (is_busy and is_busy()):
                        collected, speaking, quiet = [], False, 0.0
                        continue

                    if level > max(floor * 3.0, 0.012):
                        speaking, quiet = True, 0.0
                        collected.append(chunk.copy())
                        if len(collected) * block > max_samples:
                            collected, speaking = [], False   # a sentence, not a phrase
                    elif speaking:
                        quiet += 0.02
                        collected.append(chunk.copy())
                        if quiet < self.END_SILENCE_S:
                            continue

                        audio = np.concatenate(collected, axis=0).flatten()
                        collected, speaking, quiet = [], False, 0.0
                        if time.time() - last_fire < self.COOLDOWN_S:
                            continue
                        try:
                            hit = self.matches(audio)
                        except Exception:  # noqa: BLE001 - never kill the thread
                            continue
                        if hit:
                            last_fire = time.time()
                            try:
                                on_wake()
                            except Exception as exc:  # noqa: BLE001
                                self.last_error = f"wake handler failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"the microphone stopped: {exc}"
        finally:
            self.listening = False


wake = WakeWord()
