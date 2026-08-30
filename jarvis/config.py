"""Single source of truth for every tunable in JARVIS.

Google deprecates model IDs on a schedule that has nothing to do with your
project. Everything version-sensitive lives here so a deprecation is a
one-line change rather than an archaeology expedition.

Run `python -m jarvis.doctor` to check these IDs against the live API.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

def _root() -> Path:
    """Where this installation keeps its data.

    Under a PyInstaller bundle `__file__` points into a temporary extraction
    directory that is deleted on exit, so writing memory, logs and .env there
    would silently lose all of it between runs. A frozen build therefore keeps
    its state in %LOCALAPPDATA%\\JARVIS, which survives, is writable without
    administrator rights, and does not sit inside Program Files.

    Running from source keeps everything in the project folder, where it is
    easy to inspect.
    """
    if getattr(sys, "frozen", False):
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        home = Path(base) / "JARVIS"
        home.mkdir(parents=True, exist_ok=True)
        return home
    return Path(__file__).resolve().parent.parent


ROOT = _root()
IS_FROZEN = bool(getattr(sys, "frozen", False))

# Where the executable itself lives, for a bundled build. Someone who drops a
# .env next to the exe reasonably expects it to be read.
BUNDLE_DIR = (
    Path(sys.executable).resolve().parent if IS_FROZEN
    else Path(__file__).resolve().parent.parent
)

load_dotenv(ROOT / ".env")
if IS_FROZEN:
    load_dotenv(BUNDLE_DIR / ".env")  # does not override what is already set

ENV_FILE = ROOT / ".env"

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

# Folders the file tools may touch besides the workspace.
#
# A single workspace folder is the safe default, but it makes "put a file on
# my desktop" impossible, which is most of what a desktop assistant gets asked
# to do. These are the ordinary document folders -- never system directories,
# and never the whole drive.
#
# Set JARVIS_EXTRA_ROOTS to a semicolon-separated list to change them, or to
# an empty string to lock everything back down to the workspace alone.
_DEFAULT_EXTRA_ROOTS = "Desktop;Documents;Downloads"
_HOME = Path.home()


def _resolve_root(name: str) -> Path | None:
    """Find a user folder, following OneDrive folder redirection.

    On most Windows machines with OneDrive enabled, Desktop and Documents do
    not live under the home directory at all -- they are redirected into
    %OneDrive%. Checking only ~/Desktop silently yields nothing, and the
    assistant then insists it cannot reach your desktop while looking at a
    path that does exist.
    """
    explicit = Path(name).expanduser()
    if explicit.is_absolute():
        try:
            return explicit.resolve() if explicit.is_dir() else None
        except OSError:
            return None

    onedrive = os.getenv("OneDrive") or os.getenv("OneDriveConsumer") or ""
    candidates = [
        _HOME / name,
        *( [Path(onedrive) / name] if onedrive else [] ),
        _HOME / "OneDrive" / name,
    ]
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


EXTRA_ROOTS: list[Path] = [
    p
    for p in (
        _resolve_root(name.strip())
        for name in os.getenv("JARVIS_EXTRA_ROOTS", _DEFAULT_EXTRA_ROOTS).split(";")
        if name.strip()
    )
    if p is not None
]

ALLOWED_ROOTS: list[Path] = [WORKSPACE, *EXTRA_ROOTS]

# Files that stay unreadable no matter which root they sit under. Widening the
# roots to include Desktop would otherwise expose this project's own .env
# whenever JARVIS is checked out onto the Desktop -- which is exactly where it
# is. Matched on the filename, case-insensitively.
SENSITIVE_FILE_PATTERNS = [
    # ".env" alone missed "prod.env": cover the dotfile, suffixed and prefixed
    # forms. An audit caught that one being readable.
    ".env", ".env.*", "*.env", "env.*",
    "*.pem", "*.key", "*.pfx", "*.p12", "*.crt", "*.cer", "*.jks", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.ppk",
    "credentials*.json", "client_secret*.json", "*token*.json", "*secret*.json",
    "service-account*.json", "*credential*",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd",
    "*.kdbx", "wallet.dat", "*.ovpn", "known_hosts", "authorized_keys",
]

# Directory names that stay off-limits wherever they appear in a path.
SENSITIVE_DIR_NAMES = {
    ".ssh", ".aws", ".gnupg", ".git", ".venv",
    "node_modules", "AppData", "__pycache__",
}


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
    # Driving the pointer and keyboard is acting as the user. It stays in the
    # destructive tier even with prompts off, so the taint guard still covers
    # it: a poisoned page must not be able to click Confirm on your behalf.
    "click_mouse",
    "drag_mouse",
    "type_text",
    "press_keys",
    "move_mouse",
    "scroll_mouse",
    # Messaging as the user. A sent message cannot be recalled, and with
    # approval prompts off the taint guard is what stops an injected page
    # from messaging contacts on your behalf.
    "send_whatsapp",
    # Sending mail is outbound and cannot be recalled. Drafting is not here on
    # purpose: a draft the user reads before sending is not an escalation, and
    # blocking it would make the feature useless exactly when it is safe.
    "write_email",
    "auto_decline_calls",
    "decline_whatsapp_call",
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
    # A web page, an email, a shared document -- none of it authored here.
    "read_page",
}

# always | smart | never
#
# Defaults to `never` at the owner's explicit request: destructive tools run
# without asking. Two things survive that choice, deliberately:
#
#   * SHELL_HARD_DENY below. It never prompts -- it refuses -- so switching
#     prompts off does not touch it.
#   * TAINT_GUARD, immediately after. Approval prompts existed mostly to stop
#     a poisoned web page from turning "summarise this" into "delete that".
#     With prompts gone, this is the last thing standing between the two.
APPROVAL_MODE = os.getenv("JARVIS_APPROVAL", "never")

# When content matching injection signatures has been ingested, still ask
# before a destructive action. This is the only prompt left in normal
# operation, and it fires only when an actual attack signature was seen --
# not on every file write. Set JARVIS_TAINT_GUARD=0 for no prompts, ever.
TAINT_GUARD = os.getenv("JARVIS_TAINT_GUARD", "1") == "1"
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
    # PowerShell spells destruction differently. An audit found
    # "Format-Volume -DriveLetter C" sailing straight past the cmd-style
    # "format c:" rule, so the cmdlet vocabulary is covered explicitly.
    r"\bFormat-Volume\b",
    r"\bClear-Disk\b",
    r"\bInitialize-Disk\b",
    r"\bRemove-Partition\b",
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b[^\n]*-Force",
    r"\bRemove-Item\b[^\n]*-Recurse[^\n]*-Force[^\n]*[a-zA-Z]:[\\/]?\s*$",
    r"\bSet-ExecutionPolicy\b[^\n]*Unrestricted",
    # Turning off the machine's defences is never a routine request.
    r"\bSet-MpPreference\b[^\n]*Disable",
    r"\bAdd-MpPreference\b[^\n]*ExclusionPath",
    r"\bnetsh\b[^\n]*firewall[^\n]*\b(off|disable)\b",
    r"\btakeown\b[^\n]*/[fF]\s+[a-zA-Z]:",
    r"\bicacls\b[^\n]*[a-zA-Z]:[\\/]?\s+/grant",
    r"\bwmic\b[^\n]*shadowcopy[^\n]*delete",
    r"\bfsutil\b[^\n]*deletejournal",
    r"\bUninstall-WindowsFeature\b",
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
    # Wake word. Off until one is recorded -- there is nothing to listen for
    # before that, and a microphone open for no reason is worth avoiding.
    wake_enabled: bool = os.getenv("JARVIS_WAKEWORD", "1") == "1"
    # 0 is strict (fewer false wakes, more missed ones), 1 is eager.
    wake_sensitivity: float = float(os.getenv("JARVIS_WAKE_SENSITIVITY", "0.5"))


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
# Generation models. Both are listed by the API and both 429 on a free-tier
# key -- measured, with a text call succeeding on the same key either side of
# the attempt. They work the moment billing is enabled, and are overridable
# because Google retires these IDs faster than the text ones.
IMAGE_MODEL = os.getenv("JARVIS_IMAGE_MODEL", "gemini-2.5-flash-image")
VIDEO_MODEL = os.getenv("JARVIS_VIDEO_MODEL", "veo-3.1-fast-generate-preview")

API_KEY_ENV = "GEMINI_API_KEY"
HUD_ENABLED = os.getenv("JARVIS_HUD", "1") == "1"
LOG_LEVEL = os.getenv("JARVIS_LOG_LEVEL", "INFO")


def api_key_present() -> bool:
    return bool(os.getenv(API_KEY_ENV, "").strip())
