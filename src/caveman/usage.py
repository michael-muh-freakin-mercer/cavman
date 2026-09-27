"""Provider-neutral model usage normalization and pre-call budget checks.

This module deliberately contains no provider calls. Providers and the Agents SDK
supply usage metadata to these typed helpers; the durable orchestrator remains the
authority that persists records and events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class UsageBudgetExceeded(ValueError):
    """Raised before a model call when its declared limits would be exceeded."""


@dataclass(frozen=True)
class UsageBudget:
    """A conservative pre-call token/call budget."""

    max_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None

    def check(self, *, calls_used: int, input_tokens_used: int,
            output_tokens_used: int, total_tokens_used: int,
            requested_input_tokens: int | None = None,
            requested_output_tokens: int | None = None) -> None:
        requested_input_tokens = requested_input_tokens or 0
        requested_output_tokens = requested_output_tokens or 0
        requested_total = requested_input_tokens + requested_output_tokens
        if self.max_calls is not None and calls_used + 1 > self.max_calls:
            raise UsageBudgetExceeded("Model-call budget exhausted")
        if (self.max_input_tokens is not None and
                input_tokens_used + requested_input_tokens > self.max_input_tokens):
            raise UsageBudgetExceeded("Input-token budget exhausted")
        if (self.max_output_tokens is not None and
                output_tokens_used + requested_output_tokens > self.max_output_tokens):
            raise UsageBudgetExceeded("Output-token budget exhausted")
        if (self.max_total_tokens is not None and
                total_tokens_used + requested_total > self.max_total_tokens):
            raise UsageBudgetExceeded("Total-token budget exhausted")


def usage_mapping(value: object | None) -> dict[str, object]:
    """Copy provider usage metadata without requiring a provider SDK type."""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "__dict__"):
        return {key: val for key, val in vars(value).items() if not key.startswith("_")}
    return {}


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def token_counts(value: object | None) -> tuple[int | None, int | None, int | None]:
    """Return input, output, total counts, preserving unknown values as None."""
    usage = usage_mapping(value)
    input_tokens = _integer(usage.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _integer(usage.get("input_tokens"))
    output_tokens = _integer(usage.get("completion_tokens"))
    if output_tokens is None:
        output_tokens = _integer(usage.get("output_tokens"))
    total_tokens = _integer(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _integer(usage.get("total"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens
