"""Durable accounting at the SDK model boundary, before response validation."""
from __future__ import annotations

import logging

from agents.models.interface import Model

from .models import ModelUsageRecord
from .usage import UsageBudget


class UsageRecordingModel(Model):
    """Record each SDK model attempt with trusted, immutable-in-practice identity.

    Each nested agent gets its own wrapper. SDK transport retries internal to the
    provider client are not individually observable at this boundary.
    """

    def __init__(self, wrapped, core, run_id: str, *, provider: str, model: str,
                 role: str, task_id=None, assignment_id=None, worker_id=None,
                 budget: UsageBudget | None = None):
        self.wrapped = wrapped
        self.core = core
        self.run_id = run_id
        self.budget = budget
        self.identity = dict(provider=provider, model=model, role=role,
                             task_id=task_id, assignment_id=assignment_id,
                             worker_id=worker_id)

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, *, previous_response_id,
                           conversation_id, prompt):
        if self.budget is not None:
            # Pre-call enforcement: sum the run's already-recorded usage and
            # raise before the wrapped model is called. A rejected call never
            # happened, so no usage row is recorded for it.
            records = self.core.get_run(self.run_id).usage_records
            self.budget.check(
                calls_used=len(records),
                input_tokens_used=sum(r.input_tokens or 0 for r in records),
                output_tokens_used=sum(r.output_tokens or 0 for r in records),
                total_tokens_used=sum(r.total_tokens or 0 for r in records),
            )
        # Preserve field presence before the SDK replaces absent provider usage
        # with zero counters. Do not mutate shared agent settings.
        from dataclasses import replace

        settings = replace(model_settings, preserve_raw_usage=True)
        try:
            response = await self.wrapped.get_response(
                system_instructions=system_instructions, input=input,
                model_settings=settings, tools=tools, output_schema=output_schema,
                handoffs=handoffs, tracing=tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id, prompt=prompt,
            )
        except BaseException:
            # Failure does not prove whether the provider billed the request.
            try:
                self.core.record_usage(self.run_id, record=ModelUsageRecord(
                    run_id=self.run_id, **self.identity, usage_known=False,
                    unknown_reason="SDK model attempt failed before returning usage; billing unknown",
                ))
            except Exception:
                logging.getLogger(__name__).exception("Could not persist failed model-attempt usage")
            raise
        self.core.record_usage(self.run_id, raw_usage=getattr(response, "raw_usage", None),
                               **self.identity)
        return response

    async def stream_response(self, *args, **kwargs):
        # Caveman's durable runtime uses Runner.run, never Runner.run_streamed.
        raise NotImplementedError("Durable usage accounting does not support streaming")
        yield  # Keep the SDK's asynchronous-iterator interface.

    def get_retry_advice(self, request):
        return self.wrapped.get_retry_advice(request)

    async def close(self):
        await self.wrapped.close()

    async def _cleanup_on_run_end(self, owner):
        await self.wrapped._cleanup_on_run_end(owner)
