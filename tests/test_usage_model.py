import asyncio

import pytest

pytest.importorskip("agents")

from agents import Usage
from agents.items import ModelResponse
from agents.model_settings import ModelSettings

from caveman.orchestration import Orchestrator
from caveman.store import SQLiteStore
from caveman.usage import UsageBudget, UsageBudgetExceeded
from caveman.usage_model import UsageRecordingModel


class StubModel:
    """Wrapped model that records invocations and returns fixed usage."""

    def __init__(self, raw_usage):
        self.raw_usage = raw_usage
        self.calls = []

    async def get_response(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(
            output=[],
            usage=Usage(requests=1, input_tokens=5, output_tokens=3, total_tokens=8),
            response_id=None,
            raw_usage=self.raw_usage,
        )


def _fixture(budget=None, raw_usage=None):
    store = SQLiteStore()
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    wrapped = StubModel(raw_usage if raw_usage is not None
                        else {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
    model = UsageRecordingModel(wrapped, core, run.id, provider="openrouter",
                                model="test-model", role="worker", budget=budget)
    return store, core, run, wrapped, model


def _call(model):
    return model.get_response(
        system_instructions=None, input="hello", model_settings=ModelSettings(),
        tools=[], output_schema=None, handoffs=[], tracing=None,
        previous_response_id=None, conversation_id=None, prompt=None)


def test_exhausted_budget_rejects_before_wrapped_model_and_records_nothing():
    store, core, run, wrapped, model = _fixture(budget=UsageBudget(max_calls=1))
    core.record_usage(run.id, provider="openrouter", model="test-model", role="worker",
                      raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    with pytest.raises(UsageBudgetExceeded, match="Model-call budget exhausted"):
        asyncio.run(_call(model))

    assert wrapped.calls == []
    assert len(core.get_run(run.id).usage_records) == 1
    store.close()


def test_available_budget_allows_call_and_normal_recording():
    store, core, run, wrapped, model = _fixture(budget=UsageBudget(max_calls=2))

    response = asyncio.run(_call(model))

    assert len(wrapped.calls) == 1
    assert response.raw_usage == wrapped.raw_usage
    records = core.get_run(run.id).usage_records
    assert len(records) == 1
    assert records[0].input_tokens == 5
    assert records[0].output_tokens == 3
    assert records[0].total_tokens == 8
    store.close()


def test_no_budget_preserves_prior_recording_behavior():
    store, core, run, wrapped, model = _fixture(budget=None)

    response = asyncio.run(_call(model))

    assert len(wrapped.calls) == 1
    assert response.raw_usage == wrapped.raw_usage
    records = core.get_run(run.id).usage_records
    assert len(records) == 1
    assert records[0].run_id == run.id
    assert records[0].provider == "openrouter"
    assert records[0].role == "worker"
    assert records[0].usage_known is True
    store.close()
