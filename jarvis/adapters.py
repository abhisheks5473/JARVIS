"""Translating between Gemini's Interactions shapes and everyone else's.

The agent is written against one conversation format: typed history entries
holding lists of parts, and steps carrying `.type`, `.name`, `.arguments` and
`.id`. That format is Gemini's. Rewriting the agent per provider would mean
three agents to keep in step, so the others are translated at the edge and the
agent never learns they exist.

**Two translations cover six providers.** OpenAI's chat-completions format is
spoken by OpenAI, Groq, OpenRouter, Together, Ollama and LM Studio, so one
adapter serves all of them and only the base URL differs. Anthropic's Messages
API is close enough in spirit to be its own small case.

**Honest status.** The Gemini path is the one this project runs on and is
exercised constantly. These two are translated carefully and unit-tested in
both directions against real declarations and real history, but no live call
has been made through either -- there was no key to make one with. The
translation is verified; the round trip against a running service is not.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    """A step in the shape the agent already understands.

    The agent reads `.type`, and for a call `.name`, `.arguments` and `.id`.
    Producing the same duck type means nothing downstream has to branch on
    which provider answered.
    """

    type: str
    name: str = ""
    arguments: dict = field(default_factory=dict)
    id: str = ""
    text: str = ""


def _parts_text(entry: dict, key: str = "content") -> str:
    """The text out of an Interactions-style entry, ignoring other parts."""
    parts = entry.get(key) or []
    if isinstance(parts, str):
        return parts
    return " ".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ).strip()


# ------------------------------------------------------------------- OpenAI
def to_openai_tools(declarations: list[dict]) -> list[dict]:
    """Tool declarations into OpenAI's nested `function` wrapper."""
    tools = []
    for decl in declarations or []:
        if decl.get("type") != "function" or not decl.get("name"):
            continue          # built-ins like google_search have no equivalent
        tools.append({
            "type": "function",
            "function": {
                "name": decl["name"],
                "description": decl.get("description", ""),
                "parameters": decl.get("parameters")
                or {"type": "object", "properties": {}},
            },
        })
    return tools


def to_openai_messages(history: Any, system_instruction: str = "") -> list[dict]:
    """Interactions history into OpenAI chat messages.

    Tool calls and their results must stay paired and in order: OpenAI rejects
    a `tool` message whose `tool_call_id` it has not just seen, so a result
    whose call was dropped is dropped with it rather than sent alone.
    """
    messages: list[dict] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    if isinstance(history, str):
        messages.append({"role": "user", "content": history})
        return messages

    seen_calls: set[str] = set()
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")

        if kind == "user_input":
            messages.append({"role": "user", "content": _parts_text(entry)})

        elif kind == "model_output":
            text = _parts_text(entry)
            if text:
                messages.append({"role": "assistant", "content": text})

        elif kind == "function_call":
            call_id = str(entry.get("id") or f"call_{len(seen_calls)}")
            seen_calls.add(call_id)
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": entry.get("name", ""),
                        "arguments": json.dumps(entry.get("arguments") or {}),
                    },
                }],
            })

        elif kind == "function_result":
            call_id = str(entry.get("call_id") or "")
            if call_id not in seen_calls:
                continue      # an orphan result is a 400, not a message
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _parts_text(entry, "result") or "{}",
            })

    return messages


def from_openai_response(response: Any) -> tuple[str, list[Step], dict]:
    """An OpenAI response into (text, steps, usage)."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "", [], {}

    message = choices[0].message
    steps: list[Step] = []

    text = (getattr(message, "content", "") or "").strip()
    if text:
        steps.append(Step(type="text", text=text))

    for call in getattr(message, "tool_calls", None) or []:
        raw = getattr(call.function, "arguments", "") or "{}"
        try:
            arguments = json.loads(raw)
        except (ValueError, TypeError):
            # A model emitting malformed JSON should produce a tool error the
            # agent can report, not an exception that kills the turn.
            arguments = {"__unparsed__": str(raw)[:500]}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        steps.append(Step(
            type="function_call",
            name=getattr(call.function, "name", ""),
            arguments=arguments,
            id=str(getattr(call, "id", "") or ""),
        ))

    usage_obj = getattr(response, "usage", None)
    usage = {
        "input": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        "output": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        "total": int(getattr(usage_obj, "total_tokens", 0) or 0),
    }
    return text, steps, usage


# ---------------------------------------------------------------- Anthropic
def to_anthropic_tools(declarations: list[dict]) -> list[dict]:
    tools = []
    for decl in declarations or []:
        if decl.get("type") != "function" or not decl.get("name"):
            continue
        tools.append({
            "name": decl["name"],
            "description": decl.get("description", ""),
            "input_schema": decl.get("parameters")
            or {"type": "object", "properties": {}},
        })
    return tools


def to_anthropic_messages(history: Any) -> list[dict]:
    """Interactions history into Anthropic messages.

    Anthropic carries tool results as a `user` message holding tool_result
    blocks rather than a role of its own, so consecutive results merge into
    one message instead of alternating badly.
    """
    if isinstance(history, str):
        return [{"role": "user", "content": history}]

    messages: list[dict] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")

        if kind == "user_input":
            messages.append({"role": "user", "content": _parts_text(entry)})

        elif kind == "model_output":
            text = _parts_text(entry)
            if text:
                messages.append({"role": "assistant", "content": text})

        elif kind == "function_call":
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": str(entry.get("id") or "call"),
                    "name": entry.get("name", ""),
                    "input": entry.get("arguments") or {},
                }],
            })

        elif kind == "function_result":
            block = {
                "type": "tool_result",
                "tool_use_id": str(entry.get("call_id") or "call"),
                "content": _parts_text(entry, "result") or "{}",
            }
            if entry.get("is_error"):
                block["is_error"] = True
            if messages and messages[-1]["role"] == "user" and isinstance(
                messages[-1].get("content"), list
            ):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})

    return messages


def from_anthropic_response(response: Any) -> tuple[str, list[Step], dict]:
    steps: list[Step] = []
    chunks: list[str] = []

    for block in getattr(response, "content", None) or []:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                chunks.append(text)
                steps.append(Step(type="text", text=text))
        elif block_type == "tool_use":
            arguments = getattr(block, "input", None) or {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            steps.append(Step(
                type="function_call",
                name=getattr(block, "name", ""),
                arguments=arguments,
                id=str(getattr(block, "id", "") or ""),
            ))

    usage_obj = getattr(response, "usage", None)
    used_in = int(getattr(usage_obj, "input_tokens", 0) or 0)
    used_out = int(getattr(usage_obj, "output_tokens", 0) or 0)
    return (
        " ".join(chunks).strip(),
        steps,
        {"input": used_in, "output": used_out, "total": used_in + used_out},
    )
