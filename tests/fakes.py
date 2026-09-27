"""Offline scripted Agents SDK model fakes for Caveman's durable-runtime tests.

Wraps the pinned SDK's agents.testing.ScriptedModel so tests drive the full
Runner loop without provider credits. Every scripted step carries a raw
provider-usage payload so UsageRecordingModel persists deterministic records.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from agents.testing import ScriptedModel, assistant_message, function_call
from agents.usage import Usage

INPUT_TOKENS = 11
OUTPUT_TOKENS = 7
TOTAL_TOKENS = INPUT_TOKENS + OUTPUT_TOKENS

# Provider-shaped raw usage preserved by UsageRecordingModel (prompt/completion
# aliases exercise the durable normalization path).
RAW_USAGE = {
    "prompt_tokens": INPUT_TOKENS,
    "completion_tokens": OUTPUT_TOKENS,
    "total_tokens": TOTAL_TOKENS,
}

DEFAULT_USAGE = Usage(
    requests=1,
    input_tokens=INPUT_TOKENS,
    output_tokens=OUTPUT_TOKENS,
    total_tokens=TOTAL_TOKENS,
)


def tool_step(name: str, arguments: Mapping[str, Any] | str, *, call_id: str) -> dict:
    """One scripted turn that calls a single function tool."""
    return {
        "output": [function_call(name, arguments, call_id=call_id)],
        "raw_usage": dict(RAW_USAGE),
    }


def message_step(text: str) -> dict:
    """One scripted final assistant-message turn."""
    return {
        "output": [assistant_message(text)],
        "raw_usage": dict(RAW_USAGE),
    }


def responder_step(responder: Callable) -> dict:
    """One scripted turn computed from the live call (may read durable state)."""
    return {"responder": responder}


def scripted_model(steps: Iterable) -> ScriptedModel:
    """A deterministic offline Model with Caveman-compatible usage payloads."""
    return ScriptedModel(steps, default_usage=DEFAULT_USAGE)
