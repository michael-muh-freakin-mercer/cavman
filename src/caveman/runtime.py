from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from agents import Agent, OpenAIChatCompletionsModel, WebSearchTool, set_tracing_disabled
from openai import AsyncOpenAI

from .contracts import ToolPolicy
from .usage import UsageBudget


DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MANAGER_MODEL = "moonshotai/kimi-k3"
DEFAULT_WORKER_MODEL = "moonshotai/kimi-k3"


class RuntimeConfigurationError(ValueError):
    """Raised when Caveman's provider configuration is missing or invalid."""


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _optional_non_negative_int(values: Mapping[str, str], name: str) -> int | None:
    raw = values.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        raise RuntimeConfigurationError(
            f"{name} must be a non-negative integer; got an empty value."
        )
    try:
        parsed = int(text)
    except ValueError:
        raise RuntimeConfigurationError(
            f"{name} must be a non-negative integer; got {raw!r}."
        ) from None
    if parsed < 0:
        raise RuntimeConfigurationError(
            f"{name} must be a non-negative integer; got {raw!r}."
        )
    return parsed


DEFAULT_WORKER_MAX_TURNS = 24


@dataclass(frozen=True)
class RuntimeConfig:
    """Provider-neutral runtime settings resolved from the environment."""

    provider: str
    api_key: str
    base_url: str
    manager_model: str
    worker_model: str
    budget: UsageBudget | None = None
    worker_max_turns: int = DEFAULT_WORKER_MAX_TURNS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeConfig":
        values = os.environ if env is None else env
        provider = values.get(
            "CAVEMAN_MODEL_PROVIDER",
            values.get("CAVEMAN_PROVIDER", "openrouter"),
        ).strip().lower()
        if provider != "openrouter":
            raise RuntimeConfigurationError(
                f"Unsupported CAVEMAN_MODEL_PROVIDER={provider!r}; only 'openrouter' is supported."
            )

        api_key = values.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeConfigurationError(
                "OPENROUTER_API_KEY is not set. Add it to the environment or a local .env file."
            )
        if api_key == "your_openrouter_key_here":
            raise RuntimeConfigurationError(
                "OPENROUTER_API_KEY is still the .env.example placeholder. "
                "Set a real OpenRouter key before starting a provider-backed run."
            )

        base_url = _normalize_base_url(
            values.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL)
        )
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeConfigurationError(
                "OPENROUTER_BASE_URL must be an absolute HTTPS URL."
            )

        manager_model = values.get("CAVEMAN_MODEL", DEFAULT_MANAGER_MODEL).strip()
        worker_model = values.get("CAVEMAN_WORKER_MODEL", DEFAULT_WORKER_MODEL).strip()
        if not manager_model or not worker_model:
            raise RuntimeConfigurationError(
                "CAVEMAN_MODEL and CAVEMAN_WORKER_MODEL must both be non-empty."
            )

        budget_values = {
            "max_calls": _optional_non_negative_int(values, "CAVEMAN_MAX_MODEL_CALLS"),
            "max_input_tokens": _optional_non_negative_int(values, "CAVEMAN_MAX_INPUT_TOKENS"),
            "max_output_tokens": _optional_non_negative_int(values, "CAVEMAN_MAX_OUTPUT_TOKENS"),
            "max_total_tokens": _optional_non_negative_int(values, "CAVEMAN_MAX_TOTAL_TOKENS"),
        }
        budget = (
            UsageBudget(**budget_values)
            if any(value is not None for value in budget_values.values())
            else None
        )

        worker_max_turns = _optional_non_negative_int(values, "CAVEMAN_WORKER_MAX_TURNS")
        if worker_max_turns is not None and worker_max_turns < 1:
            raise RuntimeConfigurationError(
                "CAVEMAN_WORKER_MAX_TURNS must be at least 1."
            )

        return cls(provider, api_key, base_url, manager_model, worker_model, budget,
                   worker_max_turns or DEFAULT_WORKER_MAX_TURNS)


def _should_replay_reasoning_content(context: object, base_url: str) -> bool:
    """Replay reasoning only for a valid OpenRouter request context."""

    normalized_base_url = _normalize_base_url(base_url)
    parsed = urlparse(normalized_base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if context is None:
        return False

    context_url = getattr(context, "base_url", None)
    if isinstance(context, Mapping):
        context_url = context.get("base_url")
    if context_url is not None and _normalize_base_url(str(context_url)) != normalized_base_url:
        return False

    host = (parsed.hostname or "").lower()
    return (
        host == "openrouter.ai"
        or host.endswith(".openrouter.ai")
        or (context_url is not None and _normalize_base_url(str(context_url)) == normalized_base_url)
    )


@lru_cache(maxsize=8)
def _openrouter_client(config: RuntimeConfig) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)


def _openrouter_model(model_name: str, config: RuntimeConfig) -> OpenAIChatCompletionsModel:
    return OpenAIChatCompletionsModel(
        model=model_name,
        openai_client=_openrouter_client(config),
        should_replay_reasoning_content=lambda context: _should_replay_reasoning_content(
            context, config.base_url
        ),
    )


def build_models(config: RuntimeConfig) -> tuple[OpenAIChatCompletionsModel, OpenAIChatCompletionsModel]:
    if config.provider != "openrouter":
        raise RuntimeConfigurationError(
            f"Unsupported CAVEMAN_MODEL_PROVIDER={config.provider!r}; only 'openrouter' is supported."
        )
    set_tracing_disabled(True)
    return (
        _openrouter_model(config.manager_model, config),
        _openrouter_model(config.worker_model, config),
    )


def _agent(
    name: str,
    instructions: str,
    *,
    output_type=None,
    tools=None,
    model=None,
):
    kwargs = {
        "name": name,
        "instructions": instructions,
    }
    if output_type is not None:
        kwargs["output_type"] = output_type
    if tools is not None:
        kwargs["tools"] = tools
    if model is not None:
        kwargs["model"] = model
    return Agent(**kwargs)


def _tools_for(policy: ToolPolicy) -> list:
    if policy == "web_search":
        return [WebSearchTool(search_context_size="medium")]
    return []


def _trace_sensitive_enabled() -> bool:
    value = os.getenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_caveman(controller) -> Agent:
    """Build the Caveman Manager using the repository doctrine plus the Agents SDK adapter."""

    config = RuntimeConfig.from_env()
    manager_model, _ = build_models(config)
    from .usage_model import UsageRecordingModel

    manager_model = UsageRecordingModel(
        manager_model, controller.core, controller.run_id,
        provider=config.provider, model=config.manager_model, role="manager",
        budget=config.budget,
    )
    return _agent(
        name="Caveman",
        instructions=controller.instructions(),
        tools=controller.tools(),
        model=manager_model,
    )
