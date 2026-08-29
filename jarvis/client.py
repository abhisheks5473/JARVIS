"""The one place that talks to Google.

Everything the guide warns about lives here so it cannot be forgotten
somewhere else:

  * `tools`, `system_instruction` and `generation_config` are interaction-
    scoped -- they are NOT remembered between calls. Forget to re-send them
    and the agent mysteriously loses its personality and its tools halfway
    through a conversation. This module always re-sends them.
  * Exponential backoff with jitter on 429/500/503. A bare retry loop turns
    one 429 into fifty and gets you limited harder.
  * Safety filters cannot be tuned on the Interactions API, so a blocked or
    empty response must be handled rather than crashed on.
  * `output_text` is a convenience, not the truth: it skips text separated by
    non-text steps. When output is interleaved, walk `steps` yourself.
  * Every call is metered through the quota governor before it goes out and
    recorded after it comes back, successes and failures alike.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterator

from google import genai
from google.genai import errors

from . import config
from .config import ModelTier, Models
from .quota import Mode, governor

RETRYABLE = {408, 429, 500, 502, 503, 504}


class QuotaExhausted(RuntimeError):
    """The governor refused the call. Not an API error -- we never sent it."""


class ModelBlocked(RuntimeError):
    """The response came back empty or filtered."""


@dataclass
class CallResult:
    """Everything downstream needs, with the raw interaction still attached."""

    text: str
    steps: list[Any] = field(default_factory=list)
    raw: Any = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    degraded: bool = False

    def function_calls(self) -> list[Any]:
        return [s for s in self.steps if getattr(s, "type", None) == "function_call"]

    def has_calls(self) -> bool:
        return bool(self.function_calls())


def _text_from_steps(interaction: Any) -> str:
    """Reassemble the visible text.

    `output_text` drops text blocks that sit either side of a thought, an
    image or a tool call. Walking the steps is the only reliable way to get
    everything the model actually said.
    """
    direct = getattr(interaction, "output_text", None)
    parts: list[str] = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for block in getattr(step, "content", None) or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
    joined = "".join(parts).strip()
    # Prefer the reconstruction when it found more than the convenience field.
    if joined and (not direct or len(joined) > len(direct or "")):
        return joined
    return (direct or joined or "").strip()


def _usage_of(interaction: Any) -> tuple[int, int, int, int]:
    usage = getattr(interaction, "usage", None)
    if usage is None:
        return 0, 0, 0, 0
    return (
        int(getattr(usage, "total_input_tokens", 0) or 0),
        int(getattr(usage, "total_output_tokens", 0) or 0),
        int(getattr(usage, "total_thought_tokens", 0) or 0),
        int(getattr(usage, "total_tokens", 0) or 0),
    )


class GeminiClient:
    """Thin, opinionated wrapper. Constructed once and shared."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------ lifecycle
    @property
    def sdk(self) -> genai.Client:
        if self._client is None:
            if not config.api_key_present():
                raise RuntimeError(
                    f"{config.API_KEY_ENV} is not set. Put it in .env -- get one "
                    "free at aistudio.google.com/apikey."
                )
            # The SDK reads GEMINI_API_KEY itself; passing it explicitly keeps
            # the failure mode obvious when .env has not been loaded.
            self._client = genai.Client(api_key=os.environ[config.API_KEY_ENV])
        return self._client

    # ------------------------------------------------------------ degrade
    def _effective_tier(self, tier: ModelTier, mode: Mode) -> tuple[ModelTier, bool]:
        """Trade capability for survival as the daily budget drains.

        Degrading beats dying: a terse answer from Flash-Lite is worth more
        than a 429 at 3pm.
        """
        if mode in (Mode.CONSERVE, Mode.CRITICAL) and tier.cost > Models.FAST.cost:
            return ModelTier(Models.FAST.id, "minimal", Models.FAST.cost), True
        if mode is Mode.CONSERVE and tier.thinking in ("medium", "high"):
            return ModelTier(tier.id, "low", tier.cost), True
        return tier, False

    # ------------------------------------------------------------ call
    def call(
        self,
        tier: ModelTier,
        input: Any,
        system_instruction: str | None = None,
        tools: list[dict] | None = None,
        kind: str = "agent",
        interactive: bool = True,
        thinking: str | None = None,
        tool_choice: str | None = None,
        response_format: dict | None = None,
        max_output_tokens: int | None = None,
        stream: bool = False,
        max_attempts: int = 5,
    ) -> Any:
        """One metered, retried, quota-aware call.

        Args:
            tier: Which rung of the model ladder to use. May be downgraded
                automatically when quota is tight.
            input: A string, or the history list of step dicts.
            kind: Label for the quota ledger -- agent, vision, summary, router.
            interactive: False for scheduled work, which is sacrificed first.
        """
        # ---- pre-flight: the governor decides before we spend anything.
        verdict = None
        for _ in range(6):
            verdict = governor.check(interactive=interactive)
            if verdict.allowed:
                break
            if verdict.decision.value == "deny":
                raise QuotaExhausted(verdict.reason)
            time.sleep(verdict.wait_s)
        else:
            raise QuotaExhausted("quota stayed saturated; giving up on this turn")

        effective, degraded = self._effective_tier(tier, verdict.mode)

        generation_config: dict[str, Any] = {
            # Gemini 3 reasons internally by default. That is valuable for hard
            # problems and pure waste for "the user said hello, pick a tool".
            # Never combine thinking_level with the legacy thinking_budget --
            # that is a 400.
            "thinking_level": thinking or effective.thinking
        }
        if tool_choice:
            generation_config["tool_choice"] = tool_choice
        if max_output_tokens:
            generation_config["max_output_tokens"] = max_output_tokens

        body: dict[str, Any] = {
            "model": effective.id,
            "input": input,
            "store": config.STORE_INTERACTIONS,
            "generation_config": generation_config,
        }
        # These are interaction-scoped and must ride along every single time.
        if system_instruction:
            body["system_instruction"] = system_instruction
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        if stream:
            body["stream"] = True

        started = time.time()
        last_exception: Exception | None = None

        for attempt in range(max_attempts):
            try:
                response = self.sdk.interactions.create(**body)
            except errors.APIError as exc:
                code = getattr(exc, "code", None)
                # A 429 counted against the per-minute window even though it
                # failed. Recording it keeps the governor honest.
                governor.record(
                    model=effective.id,
                    kind=kind,
                    latency_ms=int((time.time() - started) * 1000),
                    ok=False,
                    status=str(code),
                )
                last_exception = exc
                if code in RETRYABLE and attempt < max_attempts - 1:
                    time.sleep((2**attempt) + random.random())
                    continue
                self.last_error = str(exc)
                raise
            except Exception as exc:  # network, parsing, anything else
                last_exception = exc
                if attempt < max_attempts - 1:
                    time.sleep((2**attempt) + random.random())
                    continue
                self.last_error = str(exc)
                raise

            latency_ms = int((time.time() - started) * 1000)

            if stream:
                # The caller drives the event loop and records usage itself
                # once the stream completes.
                return response

            inp, out, thought, total = _usage_of(response)
            governor.record(
                model=effective.id,
                kind=kind,
                input_tokens=inp,
                output_tokens=out,
                thought_tokens=thought,
                total_tokens=total,
                latency_ms=latency_ms,
                ok=True,
            )

            steps = list(getattr(response, "steps", None) or [])
            text = _text_from_steps(response)

            # Safety filters cannot be tuned here, so an empty response is a
            # normal outcome to handle, not an exception to crash on.
            if not text and not any(
                getattr(s, "type", None) == "function_call" for s in steps
            ):
                errs = getattr(response, "errors", None)
                detail = f" ({errs})" if errs else ""
                raise ModelBlocked(
                    f"The model returned nothing usable{detail}. This is "
                    "usually a safety filter or a truncated response."
                )

            return CallResult(
                text=text,
                steps=steps,
                raw=response,
                model=effective.id,
                input_tokens=inp,
                output_tokens=out,
                thought_tokens=thought,
                total_tokens=total,
                latency_ms=latency_ms,
                degraded=degraded,
            )

        raise RuntimeError(f"exhausted retries: {last_exception}")

    # ------------------------------------------------------------ streaming
    def consume_stream(
        self, stream: Any, model: str, kind: str = "agent"
    ) -> Iterator[tuple[str, Any]]:
        """Yield (event_kind, payload) as the stream arrives.

        event_kind is one of: text, call_args, done. Speaking sentence by
        sentence as text arrives buys more perceived speed than any model
        swap -- anything over about three seconds feels broken to someone
        talking out loud.

        Tool-call arguments also arrive as deltas and must be accumulated
        before execution, which is what the call_args events carry.
        """
        started = time.time()
        final: Any = None
        try:
            for event in stream:
                etype = getattr(event, "event_type", "") or getattr(event, "type", "")
                if etype == "step.delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "text":
                        yield "text", getattr(delta, "text", "")
                    elif dtype in ("arguments", "arguments_delta"):
                        yield "call_args", delta
                elif etype == "interaction.completed":
                    final = getattr(event, "interaction", None) or event
                    yield "done", final
        finally:
            usage = _usage_of(final) if final is not None else (0, 0, 0, 0)
            governor.record(
                model=model,
                kind=kind,
                input_tokens=usage[0],
                output_tokens=usage[1],
                thought_tokens=usage[2],
                total_tokens=usage[3],
                latency_ms=int((time.time() - started) * 1000),
                ok=final is not None,
                status="" if final is not None else "stream ended without completion",
            )

    # ------------------------------------------------------------ diagnostics
    def list_models(self) -> list[str]:
        """What this key can actually see. Trust this over any blog post."""
        try:
            return sorted(
                m.name.split("/")[-1] for m in self.sdk.models.list() if m.name
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []


client = GeminiClient()
