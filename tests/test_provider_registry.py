"""The OpenAI-compatible provider registry is the single source of truth for the
family; this guards each provider's resolved config (base URL, subclass, auth,
Responses API) so a future edit can't silently break one.
"""
import pytest

from tradingagents.llm_clients.openai_client import (
    OPENAI_COMPATIBLE_PROVIDERS,
    DeepSeekChatOpenAI,
    MinimaxChatOpenAI,
    NormalizedChatOpenAI,
    is_openai_compatible,
)


@pytest.mark.unit
def test_registry_membership():
    assert is_openai_compatible("openai")
    assert is_openai_compatible("openai_compatible")  # the generic endpoint
    # native (different API) clients are intentionally NOT in the registry
    assert not is_openai_compatible("anthropic")
    assert not is_openai_compatible("google")
    assert not is_openai_compatible("azure")


@pytest.mark.unit
@pytest.mark.parametrize("provider,base_url,chat_class,responses", [
    ("openai", None, NormalizedChatOpenAI, True),
    ("xai", "https://api.x.ai/v1", NormalizedChatOpenAI, False),
    ("deepseek", "https://api.deepseek.com", DeepSeekChatOpenAI, False),
    ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", NormalizedChatOpenAI, False),
    ("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1", NormalizedChatOpenAI, False),
    ("glm", "https://api.z.ai/api/paas/v4/", NormalizedChatOpenAI, False),
    ("glm-cn", "https://open.bigmodel.cn/api/paas/v4/", NormalizedChatOpenAI, False),
    ("minimax", "https://api.minimax.io/v1", MinimaxChatOpenAI, False),
    ("minimax-cn", "https://api.minimaxi.com/v1", MinimaxChatOpenAI, False),
    ("openrouter", "https://openrouter.ai/api/v1", NormalizedChatOpenAI, False),
    ("mistral", "https://api.mistral.ai/v1", NormalizedChatOpenAI, False),
    ("kimi", "https://api.moonshot.ai/v1", NormalizedChatOpenAI, False),
    ("kimi-code", "https://api.kimi.com/coding/v1", NormalizedChatOpenAI, False),
    ("groq", "https://api.groq.com/openai/v1", NormalizedChatOpenAI, False),
    ("nvidia", "https://integrate.api.nvidia.com/v1", NormalizedChatOpenAI, False),
    ("ollama", "http://localhost:11434/v1", NormalizedChatOpenAI, False),
])
def test_registry_spec(provider, base_url, chat_class, responses):
    spec = OPENAI_COMPATIBLE_PROVIDERS[provider]
    assert spec.base_url == base_url
    assert spec.chat_class is chat_class
    assert spec.use_responses_api is responses


@pytest.mark.unit
def test_key_optionality():
    # Local/generic endpoints are key-optional; hosted APIs require a key.
    assert OPENAI_COMPATIBLE_PROVIDERS["ollama"].key_optional is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].key_optional is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].require_base_url is True
    assert OPENAI_COMPATIBLE_PROVIDERS["xai"].key_optional is False
    assert OPENAI_COMPATIBLE_PROVIDERS["kimi-code"].key_optional is False
    # OLLAMA_BASE_URL is the only base-URL env override.
    assert OPENAI_COMPATIBLE_PROVIDERS["ollama"].base_url_env == "OLLAMA_BASE_URL"


@pytest.mark.unit
def test_kimi_code_k3_selection_is_256k():
    from tradingagents.llm_clients.model_catalog import get_model_options

    for mode in ("quick", "deep"):
        values = [value for _, value in get_model_options("kimi-code", mode)]
        assert "k3-256k" in values
        assert "k3" not in values


@pytest.mark.unit
def test_kimi_code_client_uses_coding_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_API_KEY", "sk-kimi-code-test")
    from tradingagents.llm_clients.factory import create_llm_client

    llm = create_llm_client(provider="kimi-code", model="k3-256k").get_llm()
    assert type(llm).__name__ == "NormalizedChatOpenAI"
    assert str(llm.openai_api_base) == "https://api.kimi.com/coding/v1"
    key = (
        llm.openai_api_key.get_secret_value()
        if hasattr(llm.openai_api_key, "get_secret_value")
        else llm.openai_api_key
    )
    assert key == "sk-kimi-code-test"
    assert getattr(llm, "use_responses_api", False) in (False, None)
    assert getattr(llm, "reasoning_effort", None) == "high"


@pytest.mark.unit
def test_kimi_code_k3_forwards_reasoning_effort(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_API_KEY", "sk-kimi-code-test")
    from tradingagents.llm_clients.factory import create_llm_client

    llm = create_llm_client(
        provider="kimi-code", model="k3-256k", reasoning_effort="max"
    ).get_llm()
    assert getattr(llm, "reasoning_effort", None) == "max"


@pytest.mark.unit
def test_kimi_code_coding_alias_drops_reasoning_effort(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_API_KEY", "sk-kimi-code-test")
    from tradingagents.llm_clients.factory import create_llm_client

    llm = create_llm_client(
        provider="kimi-code",
        model="kimi-for-coding",
        reasoning_effort="high",
    ).get_llm()
    assert getattr(llm, "reasoning_effort", None) is None


@pytest.mark.unit
def test_kimi_code_graph_defaults_effort_high():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {"llm_provider": "kimi-code"}
    assert TradingAgentsGraph._get_provider_kwargs(graph)["reasoning_effort"] == "high"

    graph.config = {"llm_provider": "kimi-code", "openai_reasoning_effort": "max"}
    assert TradingAgentsGraph._get_provider_kwargs(graph)["reasoning_effort"] == "max"
