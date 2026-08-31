"""Which model service to talk to, and what it needs to be told.

Everything that differs between providers lives here as data: the environment
variable holding the key, the shape that key should have, where to go and get
one, and which model to use at each rung of the ladder. Adding a provider is
one entry, not a new code path.

**Three protocols cover all of them.** Gemini has its own Interactions API.
Anthropic has its own Messages API. Everyone else -- OpenAI, Groq, OpenRouter,
Together, and anything running locally through Ollama or LM Studio -- speaks
the OpenAI chat-completions format, so one adapter serves the lot and a new
OpenAI-compatible service costs a single line below.

**Model IDs go stale.** These are defaults, not promises; each can be
overridden from .env, and `python -m jarvis.doctor` asks the key itself what
it actually has rather than trusting this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str                 # short id used in .env
    label: str               # what the user sees
    kind: str                # gemini | openai | anthropic -- which adapter
    env_var: str             # where its API key lives
    console_url: str         # where to get a key
    key_hint: str            # what a valid key looks like, for the UI
    fast: str                # cheap model
    smart: str               # capable model
    base_url: str = ""       # OpenAI-compatible services only
    needs_key: bool = True   # false for anything running on this machine
    notes: str = ""
    prefixes: tuple[str, ...] = field(default_factory=tuple)


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        key="gemini",
        label="Google Gemini",
        kind="gemini",
        env_var="GEMINI_API_KEY",
        console_url="https://aistudio.google.com/apikey",
        key_hint="starts with AIza or AQ.",
        fast="gemini-3.5-flash-lite",
        smart="gemini-3.7-flash",
        prefixes=("AIza", "AQ."),
        notes="Has a free tier. The provider this app was built and tested on.",
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI",
        kind="openai",
        env_var="OPENAI_API_KEY",
        console_url="https://platform.openai.com/api-keys",
        key_hint="starts with sk-",
        base_url="https://api.openai.com/v1",
        fast="gpt-4.1-mini",
        smart="gpt-4.1",
        prefixes=("sk-",),
        notes="Paid, no free tier.",
    ),
    "anthropic": Provider(
        key="anthropic",
        label="Anthropic Claude",
        kind="anthropic",
        env_var="ANTHROPIC_API_KEY",
        console_url="https://console.anthropic.com/settings/keys",
        key_hint="starts with sk-ant-",
        fast="claude-haiku-4-5-20251001",
        smart="claude-sonnet-4-5",
        prefixes=("sk-ant-",),
        notes="Paid, no free tier.",
    ),
    "groq": Provider(
        key="groq",
        label="Groq",
        kind="openai",
        env_var="GROQ_API_KEY",
        console_url="https://console.groq.com/keys",
        key_hint="starts with gsk_",
        base_url="https://api.groq.com/openai/v1",
        fast="llama-3.1-8b-instant",
        smart="llama-3.3-70b-versatile",
        prefixes=("gsk_",),
        notes="Free tier with rate limits.",
    ),
    "openrouter": Provider(
        key="openrouter",
        label="OpenRouter",
        kind="openai",
        env_var="OPENROUTER_API_KEY",
        console_url="https://openrouter.ai/keys",
        key_hint="starts with sk-or-",
        base_url="https://openrouter.ai/api/v1",
        fast="google/gemini-2.0-flash-exp:free",
        smart="anthropic/claude-3.5-sonnet",
        prefixes=("sk-or-",),
        notes="One key, many models. Some are free.",
    ),
    "together": Provider(
        key="together",
        label="Together AI",
        kind="openai",
        env_var="TOGETHER_API_KEY",
        console_url="https://api.together.xyz/settings/api-keys",
        key_hint="a long hex string",
        base_url="https://api.together.xyz/v1",
        fast="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        smart="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "ollama": Provider(
        key="ollama",
        label="Ollama (on this machine)",
        kind="openai",
        env_var="OLLAMA_API_KEY",
        console_url="https://ollama.com/download",
        key_hint="no key needed",
        base_url="http://localhost:11434/v1",
        fast="llama3.2",
        smart="llama3.1:70b",
        needs_key=False,
        notes="Runs locally: nothing leaves the machine and nothing is billed.",
    ),
    "lmstudio": Provider(
        key="lmstudio",
        label="LM Studio (on this machine)",
        kind="openai",
        env_var="LMSTUDIO_API_KEY",
        console_url="https://lmstudio.ai",
        key_hint="no key needed",
        base_url="http://localhost:1234/v1",
        fast="local-model",
        smart="local-model",
        needs_key=False,
        notes="Runs locally: nothing leaves the machine and nothing is billed.",
    ),
}

DEFAULT = "gemini"


def active_name() -> str:
    """Which provider is selected. Falls back rather than failing."""
    chosen = os.getenv("JARVIS_PROVIDER", DEFAULT).strip().lower()
    return chosen if chosen in PROVIDERS else DEFAULT


def active() -> Provider:
    return PROVIDERS[active_name()]


def get(name: str) -> Provider | None:
    return PROVIDERS.get((name or "").strip().lower())


def key_for(provider: Provider | None = None) -> str:
    provider = provider or active()
    return os.getenv(provider.env_var, "").strip()


def key_present(provider: Provider | None = None) -> bool:
    provider = provider or active()
    return not provider.needs_key or bool(key_for(provider))


def key_problem(provider: Provider, api_key: str) -> str:
    """Why this key looks wrong, or "" if it looks usable.

    Shape only. Whether a key actually works is the provider's to decide, and
    asking costs a round trip that can hang on a bad network -- which is the
    trap the setup wizard already fell into once and had to be pulled out of.
    """
    key = (api_key or "").strip()
    if not key:
        return "" if not provider.needs_key else "Paste your key first."
    if " " in key or "\n" in key:
        return "That contains a space, so something was copied along with it."
    if len(key) < 16:
        return f"That looks too short for a {provider.label} key."
    if provider.prefixes and not key.startswith(provider.prefixes):
        expected = " or ".join(provider.prefixes)
        return f"A {provider.label} key normally starts with {expected}."
    return ""


# --------------------------------------------------------------- persistence
# Model pins written for one provider are wrong for the next: sending
# "gemini-3.5-flash-lite" to Groq fails in a way that looks like a broken key
# rather than a stale setting. Switching provider clears them so the new
# provider's own defaults apply.
_MODEL_PINS = (
    "JARVIS_MODEL_FAST", "JARVIS_MODEL_SMART", "JARVIS_MODEL_DEEP",
    "JARVIS_MODEL_VISION", "JARVIS_MODEL_SUMMARY",
)


def _read_env(path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip()
    return values


def save_choice(provider_name: str, api_key: str = "", env_file=None) -> str:
    """Select a provider, store its key, and make it live immediately.

    Writes .env and updates this process's environment, so a running app
    switches without restarting. Returns "" on success, or why not.

    Keys already saved for other providers are left alone -- someone moving
    between two services should not have to paste both again.
    """
    import os

    provider = get(provider_name)
    if provider is None:
        return f"Unknown provider: {provider_name}"

    key = (api_key or "").strip()
    if key:
        problem = key_problem(provider, key)
        if problem:
            return problem
    elif provider.needs_key and not os.getenv(provider.env_var, "").strip():
        return f"{provider.label} needs an API key."

    from . import config

    target = env_file or config.ENV_FILE
    values = _read_env(target)
    switching = values.get("JARVIS_PROVIDER", DEFAULT) != provider.key

    values["JARVIS_PROVIDER"] = provider.key
    if key:
        values[provider.env_var] = key
    if switching:
        for pin in _MODEL_PINS:
            values.pop(pin, None)
            os.environ.pop(pin, None)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# Written by JARVIS. Keep this file private.\n"
            + "\n".join(f"{k}={v}" for k, v in values.items())
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return f"Could not save settings: {exc}"

    os.environ["JARVIS_PROVIDER"] = provider.key
    if key:
        os.environ[provider.env_var] = key
    config.refresh()
    return ""
