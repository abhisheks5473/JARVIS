"""Single source of truth for every tunable in JARVIS.

Google deprecates model IDs on a schedule that has nothing to do with your
project. Everything version-sensitive lives here so a deprecation is a
one-line change rather than an archaeology expedition.

Run `python -m jarvis.doctor` to check these IDs against the live API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------- paths
WORKSPACE = Path(os.getenv("JARVIS_WORKSPACE", str(ROOT / "workspace"))).resolve()
DATA_DIR = Path(os.getenv("JARVIS_DATA", str(ROOT / "data"))).resolve()
LOG_DIR = Path(os.getenv("JARVIS_LOGS", str(ROOT / "logs"))).resolve()
TRASH_DIR = DATA_DIR / "trash"
MEMORY_DB = DATA_DIR / "memory.db"
QUOTA_DB = DATA_DIR / "quota.db"
VOICE_DIR = DATA_DIR / "voice"

for _p in (WORKSPACE, DATA_DIR, LOG_DIR, TRASH_DIR, VOICE_DIR):
    _p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- models
@dataclass(frozen=True)
class ModelTier:
    """One rung on the escalation ladder.

    `cost` is a relative weight used by the router, not a real price. It only
    has to be ordinally correct for "prefer the cheaper one" to work.
    """

    id: str
    thinking: str  # minimal | low | medium | high
    cost: int


class Models:
    # Routine turns: pick a tool, answer a short question, classify intent.
    # Flash-Lite is built for exactly this and is the cheapest thing that works.
    FAST = ModelTier(os.getenv("JARVIS_MODEL_FAST", "gemini-3.5-flash-lite"), "low", 1)

    # Anything needing real reasoning: multi-step plans, code, ambiguity.
    SMART = ModelTier(os.getenv("JARVIS_MODEL_SMART", "gemini-3.7-flash"), "medium", 4)

    # Deliberate, rare, expensive. Reserved for explicit "think hard" requests.
    DEEP = ModelTier(os.getenv("JARVIS_MODEL_DEEP", "gemini-3.7-flash"), "high", 10)

    # Vision subagent. Kept separate so screenshots never ride the main model.
    VISION = ModelTier(os.getenv("JARVIS_MODEL_VISION", "gemini-3.5-flash-lite"), "minimal", 1)

    # Summarising old conversation turns. Must be cheap or it defeats itself.
    SUMMARY = ModelTier(os.getenv("JARVIS_MODEL_SUMMARY", "gemini-3.5-flash-lite"), "minimal", 1)

    @classmethod
    def all_tiers(cls) -> tuple[ModelTier, ...]:
        return (cls.FAST, cls.SMART, cls.DEEP, cls.VISION, cls.SUMMARY)

    @classmethod
    def all_ids(cls) -> set[str]:
        return {t.id for t in cls.all_tiers()}


# ---------------------------------------------------------------- quota
# These are conservative defaults. Free-tier limits change without notice and
# are enforced per Google Cloud *project*, not per key. Check your live numbers
# in AI Studio and set them in .env -- the governor is only as good as these.
@dataclass(frozen=True)
class QuotaLimits:
    rpm: int = int(os.getenv("JARVIS_RPM", "14"))
    tpm: int = int(os.getenv("JARVIS_TPM", "240000"))
    rpd: int = int(os.getenv("JARVIS_RPD", "180"))

    # Below this fraction of the daily budget remaining, JARVIS conserves:
    # no vision, minimal thinking, fast model only.
    conserve_below: float = float(os.getenv("JARVIS_CONSERVE_AT", "0.25"))
    # Below this, only user-initiated turns get through; scheduled jobs skip.
    critical_below: float = float(os.getenv("JARVIS_CRITICAL_AT", "0.08"))


QUOTA = QuotaLimits()

# Daily quotas reset at midnight Pacific, which is mid-afternoon in India.
# The governor needs the real reset boundary or its burn-down is nonsense.
QUOTA_RESET_TZ = os.getenv("JARVIS_QUOTA_TZ", "America/Los_Angeles")


# ---------------------------------------------------------------- agent loop
MAX_STEPS = int(os.getenv("JARVIS_MAX_STEPS", "8"))
MAX_TOOL_CALLS_PER_TURN = int(os.getenv("JARVIS_MAX_TOOL_CALLS", "12"))
TURN_TIMEOUT_S = float(os.getenv("JARVIS_TURN_TIMEOUT", "120"))

# Conversation history keeps this many turns verbatim; older turns are
# compacted into a running summary.
HISTORY_KEEP_TURNS = int(os.getenv("JARVIS_HISTORY_TURNS", "12"))
HISTORY_COMPACT_AT = int(os.getenv("JARVIS_HISTORY_COMPACT_AT", "20"))

# store=False keeps conversation data off Google's servers. On the free tier,
# where prompts may be used to improve Google's products, this is the correct
# default. It disables previous_interaction_id and server-side caching.
STORE_INTERACTIONS = os.getenv("JARVIS_STORE", "0") == "1"


# ---------------------------------------------------------------- security
# Tools that can affect the world irreversibly. Gated unless explicitly
# disarmed for the session.
DESTRUCTIVE_TOOLS = {
    "run_shell",
    "run_powershell",
    "write_file",
    "delete_path",
    "move_path",
    "launch_app",
    "close_window",
    "send_keys",
    "set_clipboard",
    "lock_screen",
    "kill_process",
    "git_command",
    "run_tests",
    "media_control",
    "set_volume",
}

# Tools that ingest content JARVIS did not author. Calling one of these marks
# the conversation as tainted -- see jarvis/security/taint.py.
UNTRUSTED_SOURCE_TOOLS = {
    "web_search",
    "fetch_url",
    "read_file",
    "read_email",
    "list_calendar",
    "see_screen",
    "read_clipboard",
    "search_files",
}

APPROVAL_MODE = os.getenv("JARVIS_APPROVAL", "smart")  # always | smart | never
AUTONOMOUS_MAY_APPROVE = False  # scheduled jobs never self-approve. Non-negotiable.

# Real deletes become a move to TRASH_DIR, emptied after N days by a
# scheduled job. A 2% error rate is fine for "summarise this page". It is
# not fine for "rm -rf".
TRASH_RETENTION_DAYS = int(os.getenv("JARVIS_TRASH_DAYS", "30"))

# Shell commands matching these are refused outright, before any approval
# prompt. Defence in depth: the approval gate can be disarmed, this cannot.
SHELL_HARD_DENY = [
    r"\brm\s+-rf\s+[/~]",
    r"\bformat\s+[a-zA-Z]:",
    r"\bmkfs\b",
    r"\bdd\s+if=.*of=/dev/",
    r":\(\)\s*\{.*\}\s*;\s*:",
    r"\bcipher\s+/w",
    r"\bvssadmin\s+delete\s+shadows",
    r"\bbcdedit\b",
    r"\bdiskpart\b",
    r"\bnet\s+user\s+\S+\s+/add",
    r"\breg\s+delete\s+HKLM",
    r"\bRemove-Item\b[^\n]*-Recurse[^\n]*[Cc]:\\\\?\s*$",
    r"curl[^|]*\|\s*(bash|sh|powershell|iex)",
    r"Invoke-Expression[^\n]*DownloadString",
    r"\biwr\b[^\n]*\|\s*iex",
    r"\bshutdown\b\s+/[fr]",
]


# ---------------------------------------------------------------- voice
@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = os.getenv("JARVIS_VOICE", "1") == "1"
    stt_model: str = os.getenv("JARVIS_STT_MODEL", "base.en")
    stt_device: str = os.getenv("JARVIS_STT_DEVICE", "cpu")
    stt_compute: str = os.getenv("JARVIS_STT_COMPUTE", "int8")
    piper_voice: str = os.getenv("JARVIS_PIPER_VOICE", "en_GB-alan-medium")
    sample_rate: int = 16000
    hotkey: str = os.getenv("JARVIS_HOTKEY", "ctrl+alt+j")
    # Silence (seconds) after speech before we stop recording.
    silence_timeout: float = float(os.getenv("JARVIS_SILENCE", "1.2"))
    max_utterance_s: float = float(os.getenv("JARVIS_MAX_UTTERANCE", "30"))
    # Mute the mic during playback or it transcribes its own voice and
    # cheerfully replies to itself.
    mute_during_playback: bool = True
    barge_in: bool = os.getenv("JARVIS_BARGE_IN", "1") == "1"


VOICE = VoiceConfig()


# ---------------------------------------------------------------- integrations
@dataclass(frozen=True)
class Integrations:
    google_credentials: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "")
    google_token: str = str(DATA_DIR / "google_token.json")
    weather_location: str = os.getenv("JARVIS_LOCATION", "")
    user_name: str = os.getenv("JARVIS_USER_NAME", "sir")
    assistant_name: str = os.getenv("JARVIS_NAME", "JARVIS")
    timezone: str = os.getenv("JARVIS_TZ", "Asia/Kolkata")


INTEGRATIONS = Integrations()


# ---------------------------------------------------------------- misc
API_KEY_ENV = "GEMINI_API_KEY"
HUD_ENABLED = os.getenv("JARVIS_HUD", "1") == "1"
LOG_LEVEL = os.getenv("JARVIS_LOG_LEVEL", "INFO")


def api_key_present() -> bool:
    return bool(os.getenv(API_KEY_ENV, "").strip())
