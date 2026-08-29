"""The conscience: what JARVIS may do without asking, and what it may never do.

Guide section 1 calls this the row hobby projects skip and later regret. It is
built here in Phase 4, not "later".

Four layers, outermost first:

1. **Hard deny.** A short list of commands refused before any prompt is shown.
   This layer cannot be disarmed -- not by config, not by the model, not by
   you in a hurry. It exists because the failure mode is unrecoverable.

2. **Taint escalation.** If the conversation has ingested hostile content, the
   destructive tier requires human approval no matter what mode is set. The
   attack shape is "read a poisoned page, then act on it", so the moment the
   first half happens the second half stops being routine.

3. **Risk tier.** Tools are classified SAFE / GUARDED / DESTRUCTIVE. Only the
   top tier prompts in normal use, so the prompt keeps meaning something. An
   approval dialog you see forty times an hour is one you stop reading.

4. **Autonomy rule.** A scheduled job never self-approves. It queues the
   action and tells you. Non-negotiable, and not configurable.

Secrets never reach the model. `redact()` runs over anything that could carry
an API key into a prompt or a log. An agent that can read its own credentials
can leak them.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from collections.abc import Callable

from .. import config
from .taint import TaintLedger


class Risk(str, Enum):
    SAFE = "safe"                # read-only, local, no side effects
    GUARDED = "guarded"          # side effects, but reversible and small
    DESTRUCTIVE = "destructive"  # irreversible, outbound, or costly
    FORBIDDEN = "forbidden"      # never, under any configuration


class Outcome(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENIED = "denied"
    QUEUED = "queued"            # autonomous run parked it for you


@dataclass
class Judgement:
    outcome: Outcome
    risk: Risk
    reason: str = ""
    escalated_by_taint: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW


@dataclass
class QueuedAction:
    at: float
    tool: str
    arguments: dict
    reason: str


# ---------------------------------------------------------------- redaction
# Ordered most-specific first so a Google key is not swallowed by the generic
# assignment rule.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    # Google also issues keys in an "AQ.<base64ish>" form. An earlier version
    # of this list only knew about the AIza shape, which meant a real key of
    # this format travelled through logs and prompts unredacted. Anything
    # matching only the generic assignment rule below is caught in
    # "KEY=value" context but not when the bare token appears in tool output.
    ("google_api_key_v2", re.compile(r"\bAQ\.[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("aws_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S)),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
        r"\s*[:=]\s*[\"']?([^\s\"']{8,})[\"']?")),
]


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a marker.

    Applied to tool results before they enter the model's context and to
    everything written to the transcript log. Over-redaction is a cosmetic
    problem; under-redaction puts your keys in someone else's dataset.
    """
    if not text:
        return text
    out = text
    for name, pattern in _SECRET_PATTERNS:
        if name == "assignment":
            out = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", out)
        else:
            out = pattern.sub(f"[REDACTED:{name}]", out)
    return out


def contains_secret(text: str) -> bool:
    return bool(text) and redact(text) != text


# ---------------------------------------------------------------- classify
_HARD_DENY = [re.compile(p, re.I) for p in config.SHELL_HARD_DENY]

# Arguments that carry a command line, whichever tool they belong to.
_COMMAND_ARGS = ("command", "cmd", "script", "shell", "powershell", "code")


def shell_hard_denied(command: str) -> str | None:
    """Return the matched rule if this command must never run."""
    for pattern in _HARD_DENY:
        if pattern.search(command or ""):
            return pattern.pattern
    return None


class ApprovalGate:
    """Decides, and (when a prompter is attached) asks.

    `prompter` is injected rather than hardcoded so the CLI, the HUD and the
    test suite can each supply their own -- and so an autonomous run can
    supply none at all, which makes self-approval structurally impossible
    rather than merely discouraged.
    """

    def __init__(
        self,
        mode: str | None = None,
        prompter: Callable[[str, dict, str], bool] | None = None,
    ) -> None:
        self.mode = mode or config.APPROVAL_MODE
        self.prompter = prompter
        self.queue: list[QueuedAction] = []
        self.history: list[tuple[float, str, Outcome]] = []

    # ------------------------------------------------------------ tiering
    def classify(self, tool: str, arguments: dict | None = None) -> Risk:
        arguments = arguments or {}

        for key in _COMMAND_ARGS:
            value = arguments.get(key)
            if isinstance(value, str) and shell_hard_denied(value):
                return Risk.FORBIDDEN

        if tool in config.DESTRUCTIVE_TOOLS:
            return Risk.DESTRUCTIVE

        # A write is a write whatever the tool is called.
        path = arguments.get("path") or arguments.get("destination")
        if isinstance(path, str) and tool.startswith(("write", "delete", "move")):
            return Risk.DESTRUCTIVE

        if tool in config.UNTRUSTED_SOURCE_TOOLS:
            return Risk.GUARDED

        return Risk.SAFE

    # ------------------------------------------------------------ decide
    def evaluate(
        self,
        tool: str,
        arguments: dict | None = None,
        ledger: TaintLedger | None = None,
        interactive: bool = True,
    ) -> Judgement:
        arguments = arguments or {}
        risk = self.classify(tool, arguments)

        # Layer 1: hard deny. No prompt, no override, no exceptions.
        if risk is Risk.FORBIDDEN:
            matched = ""
            for key in _COMMAND_ARGS:
                value = arguments.get(key)
                if isinstance(value, str):
                    matched = shell_hard_denied(value) or matched
            judgement = Judgement(
                Outcome.DENIED,
                risk,
                reason=(
                    "Refused by hard-deny rule. This command is on the "
                    f"never-run list (matched: {matched}). Not promptable."
                ),
            )
            self._remember(tool, judgement)
            return judgement

        hostile = bool(ledger and ledger.is_hostile)
        tainted = bool(ledger and ledger.is_tainted)

        # Layer 2: taint escalation.
        #
        # Approval prompts are off by default now, so this is the last control
        # between a poisoned page and a destructive action. It fires only when
        # an actual injection signature was seen, which is why it survives the
        # switch-off -- it is not the "confirm every write" nagging that was
        # removed. JARVIS_TAINT_GUARD=0 disables it too.
        if hostile and risk is Risk.DESTRUCTIVE and not config.TAINT_GUARD:
            hostile = False

        if hostile and risk is Risk.DESTRUCTIVE:
            if not interactive:
                judgement = Judgement(
                    Outcome.QUEUED,
                    risk,
                    reason=(
                        "Hostile content was ingested this conversation and "
                        "this action is destructive. An unattended run will "
                        "not take it."
                    ),
                    escalated_by_taint=True,
                )
                self._queue(tool, arguments, judgement.reason)
                self._remember(tool, judgement)
                return judgement
            judgement = self._ask(
                tool,
                arguments,
                risk,
                reason=(
                    "INJECTION RISK: this conversation has ingested content "
                    "matching injection signatures, and this action is "
                    "destructive. Approve only if you asked for it yourself."
                ),
                escalated=True,
            )
            self._remember(tool, judgement)
            return judgement

        # Layer 4: autonomy. Checked before mode, because no mode setting
        # entitles a scheduled job to approve itself.
        if not interactive and risk is Risk.DESTRUCTIVE:
            judgement = Judgement(
                Outcome.QUEUED,
                risk,
                reason="Destructive action from an unattended run; queued for you.",
            )
            self._queue(tool, arguments, judgement.reason)
            self._remember(tool, judgement)
            return judgement

        # Layer 3: risk tier against the configured mode.
        if self.mode == "never":
            judgement = Judgement(Outcome.ALLOW, risk, reason="approval disabled")
        elif self.mode == "always" and risk is not Risk.SAFE:
            judgement = self._ask(tool, arguments, risk, reason="approval mode: always")
        elif risk is Risk.DESTRUCTIVE:
            judgement = self._ask(tool, arguments, risk, reason="destructive action")
        elif risk is Risk.GUARDED and tainted:
            # Reading more untrusted content once already tainted is allowed,
            # but worth surfacing rather than doing silently.
            judgement = Judgement(
                Outcome.ALLOW,
                risk,
                reason="reading further untrusted content",
                escalated_by_taint=True,
            )
        else:
            judgement = Judgement(Outcome.ALLOW, risk)

        self._remember(tool, judgement)
        return judgement

    # ------------------------------------------------------------ internals
    def _ask(
        self,
        tool: str,
        arguments: dict,
        risk: Risk,
        reason: str,
        escalated: bool = False,
    ) -> Judgement:
        if self.prompter is None:
            # No way to ask means no approval. Failing closed is the design.
            return Judgement(
                Outcome.NEEDS_APPROVAL,
                risk,
                reason=f"{reason} (no approval channel available)",
                escalated_by_taint=escalated,
            )
        # A prompter is UI, and UI breaks: closed stdin, a terminal in a
        # strange state, a HUD bug. An audit found an exception here escaping
        # all the way out and killing the turn. Fail closed instead -- a
        # crashed prompt is not consent.
        try:
            approved = bool(self.prompter(tool, arguments, reason))
        except Exception as exc:  # noqa: BLE001
            return Judgement(
                Outcome.DENIED,
                risk,
                reason=(
                    f"could not ask for approval ({type(exc).__name__}); "
                    "refusing rather than assuming consent"
                ),
                escalated_by_taint=escalated,
            )

        return Judgement(
            Outcome.ALLOW if approved else Outcome.DENIED,
            risk,
            reason=reason if approved else "You declined this action.",
            escalated_by_taint=escalated,
        )

    def _queue(self, tool: str, arguments: dict, reason: str) -> None:
        self.queue.append(
            QueuedAction(
                at=time.time(), tool=tool, arguments=dict(arguments), reason=reason
            )
        )

    def _remember(self, tool: str, judgement: Judgement) -> None:
        self.history.append((time.time(), tool, judgement.outcome))
        if len(self.history) > 500:
            del self.history[:250]

    # ------------------------------------------------------------ reporting
    def pending(self) -> list[QueuedAction]:
        return list(self.queue)

    def drain_queue(self) -> list[QueuedAction]:
        items, self.queue = list(self.queue), []
        return items

    def denial_rate(self) -> float:
        if not self.history:
            return 0.0
        denied = sum(1 for _, _, o in self.history if o is Outcome.DENIED)
        return denied / len(self.history)
