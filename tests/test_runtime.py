import pytest

pytest.importorskip("agents")

from caveman import runtime
from caveman.usage import UsageBudget


def test_config_requires_openrouter_key():
    with pytest.raises(runtime.RuntimeConfigurationError, match="OPENROUTER_API_KEY"):
        runtime.RuntimeConfig.from_env({})


def test_config_rejects_placeholder_openrouter_key():
    with pytest.raises(runtime.RuntimeConfigurationError, match="placeholder"):
        runtime.RuntimeConfig.from_env(
            {"OPENROUTER_API_KEY": "your_openrouter_key_here"})


def test_worker_max_turns_defaults_and_validates():
    default = runtime.RuntimeConfig.from_env({"OPENROUTER_API_KEY": "test"})
    assert default.worker_max_turns == runtime.DEFAULT_WORKER_MAX_TURNS
    configured = runtime.RuntimeConfig.from_env(
        {"OPENROUTER_API_KEY": "test", "CAVEMAN_WORKER_MAX_TURNS": "40"})
    assert configured.worker_max_turns == 40
    with pytest.raises(runtime.RuntimeConfigurationError, match="CAVEMAN_WORKER_MAX_TURNS"):
        runtime.RuntimeConfig.from_env(
            {"OPENROUTER_API_KEY": "test", "CAVEMAN_WORKER_MAX_TURNS": "0"})
    with pytest.raises(runtime.RuntimeConfigurationError, match="CAVEMAN_WORKER_MAX_TURNS"):
        runtime.RuntimeConfig.from_env(
            {"OPENROUTER_API_KEY": "test", "CAVEMAN_WORKER_MAX_TURNS": "many"})


def test_config_rejects_non_openrouter_provider():
    with pytest.raises(
        runtime.RuntimeConfigurationError,
        match="CAVEMAN_MODEL_PROVIDER.*only 'openrouter'",
    ):
        runtime.RuntimeConfig.from_env(
            {"CAVEMAN_MODEL_PROVIDER": "openai", "OPENROUTER_API_KEY": "test"}
        )


def test_models_are_explicitly_wired_and_tracing_disabled(monkeypatch):
    config = runtime.RuntimeConfig.from_env(
        {
            "OPENROUTER_API_KEY": "test",
            "CAVEMAN_MODEL": "manager",
            "CAVEMAN_WORKER_MODEL": "worker",
        }
    )
    clients = []
    calls = []
    runtime._openrouter_client.cache_clear()

    class FakeClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

    class FakeModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(runtime, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(runtime, "OpenAIChatCompletionsModel", FakeModel)
    monkeypatch.setattr(runtime, "set_tracing_disabled", lambda value: calls.append(value))

    manager, worker = runtime.build_models(config)

    assert manager is not None and worker is not None
    assert calls[0] is True
    assert [call["model"] for call in calls[1:]] == ["manager", "worker"]
    assert all(client["base_url"] == "https://openrouter.ai/api/v1" for client in clients)
    assert all(client["api_key"] == "test" for client in clients)
    assert all(call["should_replay_reasoning_content"](object()) for call in calls[1:])
    assert all(not call["should_replay_reasoning_content"](None) for call in calls[1:])
    assert runtime._should_replay_reasoning_content(object(), "https://openrouter.ai/api/v1/")


def test_default_models_and_reasoning_context():
    config = runtime.RuntimeConfig.from_env({"OPENROUTER_API_KEY": "test"})
    assert config.provider == "openrouter"
    assert config.manager_model == "moonshotai/kimi-k3"
    assert config.worker_model == "moonshotai/kimi-k3"
    assert config.budget is None
    assert runtime._should_replay_reasoning_content(object(), config.base_url)
    assert not runtime._should_replay_reasoning_content(None, config.base_url)
    assert not runtime._should_replay_reasoning_content(object(), "http://localhost/v1")


def test_budget_built_from_optional_env_vars():
    config = runtime.RuntimeConfig.from_env(
        {
            "OPENROUTER_API_KEY": "test",
            "CAVEMAN_MAX_MODEL_CALLS": "10",
            "CAVEMAN_MAX_INPUT_TOKENS": "1000",
            "CAVEMAN_MAX_OUTPUT_TOKENS": "2000",
            "CAVEMAN_MAX_TOTAL_TOKENS": "3000",
        }
    )
    assert config.budget == UsageBudget(
        max_calls=10, max_input_tokens=1000, max_output_tokens=2000, max_total_tokens=3000
    )


def test_partial_budget_env_vars_leave_other_limits_unset():
    config = runtime.RuntimeConfig.from_env(
        {"OPENROUTER_API_KEY": "test", "CAVEMAN_MAX_MODEL_CALLS": "3"}
    )
    assert config.budget == UsageBudget(max_calls=3)


@pytest.mark.parametrize(
    "name",
    [
        "CAVEMAN_MAX_MODEL_CALLS",
        "CAVEMAN_MAX_INPUT_TOKENS",
        "CAVEMAN_MAX_OUTPUT_TOKENS",
        "CAVEMAN_MAX_TOTAL_TOKENS",
    ],
)
@pytest.mark.parametrize("value", ["abc", "-1", "", "1.5"])
def test_malformed_budget_env_var_raises_naming_the_variable(name, value):
    with pytest.raises(runtime.RuntimeConfigurationError, match=name):
        runtime.RuntimeConfig.from_env({"OPENROUTER_API_KEY": "test", name: value})


def test_build_caveman_passes_configured_budget_to_manager_model(monkeypatch):
    from caveman.usage_model import UsageRecordingModel

    class Controller:
        core = object()
        run_id = "run-test"

        def instructions(self):
            return "durable instructions"

        def tools(self):
            return ["durable tool"]

    controller = Controller()
    manager_model, worker_model = object(), object()
    budget = UsageBudget(max_calls=7)

    monkeypatch.setattr(
        runtime.RuntimeConfig,
        "from_env",
        classmethod(lambda cls: runtime.RuntimeConfig(
            "openrouter", "key", "https://openrouter.ai/api/v1", "manager", "worker", budget
        )),
    )
    monkeypatch.setattr(runtime, "build_models", lambda value: (manager_model, worker_model))
    monkeypatch.setattr(runtime, "_agent", lambda **kwargs: kwargs)

    manager = runtime.build_caveman(controller)

    model = manager["model"]
    assert isinstance(model, UsageRecordingModel)
    assert model.budget is budget


def test_provider_client_is_reused(monkeypatch):
    config = runtime.RuntimeConfig("openrouter", "key", "https://openrouter.ai/api/v1", "m", "w")
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

    runtime._openrouter_client.cache_clear()
    monkeypatch.setattr(runtime, "AsyncOpenAI", FakeClient)
    runtime._openrouter_model("m", config)
    runtime._openrouter_model("w", config)
    assert len(clients) == 1


def test_manager_uses_durable_controller_when_supplied(monkeypatch):
    from caveman.usage_model import UsageRecordingModel

    class Controller:
        core = object()
        run_id = "run-test"

        def instructions(self):
            return "durable instructions"

        def tools(self):
            return ["durable tool"]

    controller = Controller()
    manager_model, worker_model = object(), object()

    monkeypatch.setattr(
        runtime.RuntimeConfig,
        "from_env",
        classmethod(lambda cls: runtime.RuntimeConfig(
            "openrouter", "key", "https://openrouter.ai/api/v1", "manager", "worker"
        )),
    )
    monkeypatch.setattr(runtime, "build_models", lambda value: (manager_model, worker_model))
    monkeypatch.setattr(runtime, "_agent", lambda **kwargs: kwargs)

    manager = runtime.build_caveman(controller)

    # The manager agent is driven by the controller's doctrine and tools.
    assert manager["instructions"] == "durable instructions"
    assert manager["tools"] == ["durable tool"]

    # The manager model is wrapped for durable usage accounting with the
    # controller's identity and the manager role.
    model = manager["model"]
    assert isinstance(model, UsageRecordingModel)
    assert model.wrapped is manager_model
    assert model.core is controller.core
    assert model.run_id == controller.run_id
    assert model.identity["role"] == "manager"
