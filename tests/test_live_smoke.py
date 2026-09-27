import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("agents")

from agents import Agent, Runner, SQLiteSession, function_tool

from caveman.runtime import RuntimeConfig, build_models


@pytest.mark.live
def test_openrouter_live_smoke():
    """Opt-in only: exercise tool continuation and persisted conversational history."""

    if os.getenv("CAVEMAN_LIVE_SMOKE") != "1":
        pytest.skip("set CAVEMAN_LIVE_SMOKE=1 to enable the credit-bearing smoke test")

    calls = []

    @function_tool
    async def remember_fact(fact: str) -> str:
        """Record a fact and return a deterministic tool result."""
        calls.append(fact)
        return f"FACT_RECORDED:{fact}"

    async def run():
        config = RuntimeConfig.from_env()
        manager_model, _ = build_models(config)
        agent = Agent(
            name="Caveman live smoke",
            instructions=(
                "Use remember_fact whenever asked to record something. After the tool returns, "
                "state the exact tool result and answer the user."
            ),
            model=manager_model,
            tools=[remember_fact],
        )
        session_path = Path(".local")
        session_path.mkdir(parents=True, exist_ok=True)
        session = SQLiteSession(f"live-smoke-{uuid4().hex}", str(session_path / "live-smoke.db"))
        first = await Runner.run(
            agent,
            "Use remember_fact to record the fact 'Kimi K3'. Then report the exact tool result.",
            session=session,
            max_turns=6,
        )
        assert calls == ["Kimi K3"]
        assert "FACT_RECORDED:Kimi K3" in first.final_output

        second = await Runner.run(
            agent,
            "What exact fact did the tool record in my previous turn?",
            session=session,
            max_turns=4,
        )
        assert "Kimi K3" in second.final_output
        # Successful continuation through the provider's reasoning/tool history is the
        # observable replay signal; the SDK keeps provider reasoning content internal.

    asyncio.run(run())
