import pytest
import httpx
from unittest.mock import AsyncMock, patch
from core.config import settings
from services import llm_client


def test_vllm_and_ollama_config_settings():
    assert hasattr(settings, "LLM_PROVIDER")
    assert hasattr(settings, "VLLM_BASE_URL")
    assert hasattr(settings, "VLLM_MODEL")
    assert hasattr(settings, "AI_CHAT_PROVIDER")
    assert hasattr(settings, "AI_CHAT_MODEL")
    assert settings.LLM_PROVIDER in ("ollama", "vllm")
    assert settings.AI_CHAT_PROVIDER == "ollama"
    assert settings.AI_CHAT_MODEL == "gpt-oss:120b"
    assert "8001" in settings.VLLM_BASE_URL
    assert settings.VLLM_MODEL == "Qwen/Qwen3-8B"


@pytest.mark.asyncio
async def test_ai_chat_uses_gpt_oss_via_ollama():
    """Verify that AI chat routes to gpt-oss:120b via Ollama."""
    messages = [
        {"role": "user", "content": "Need quotes for 100 tons TMT bar"}
    ]
    ollama_chat_response = httpx.Response(
        200,
        json={
            "message": {
                "role": "assistant",
                "content": "I can help you prepare an RFQ for 100 tons TMT steel bar.",
                "tool_calls": None,
            }
        },
        request=httpx.Request("POST", "http://localhost:11434/api/chat"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=ollama_chat_response) as mock_post:
        msg = await llm_client.chat(messages, None, timeout=10.0)
        assert "TMT steel bar" in msg["content"]
        assert mock_post.called
        call_url = str(mock_post.call_args[0][0])
        assert "11434/api/chat" in call_url
        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "gpt-oss:120b"


@pytest.mark.asyncio
async def test_llm_client_vllm_when_vllm_model_specified():
    """When Qwen/Qwen3-8B is explicitly targeted, it routes to vLLM on port 8001."""
    fake_response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": '{"product": "steel rebar", "quantity": 50, "unit": "ton"}'
                    }
                }
            ]
        },
        request=httpx.Request("POST", "http://127.0.0.1:8001/v1/chat/completions"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_response
        result = await llm_client.generate_json(
            prompt="parse this",
            model="Qwen/Qwen3-8B",
            timeout=10.0,
        )
        assert result == {"product": "steel rebar", "quantity": 50, "unit": "ton"}
        assert mock_post.called
        call_url = str(mock_post.call_args[0][0])
        assert "8001/v1/chat/completions" in call_url


@pytest.mark.asyncio
async def test_llm_client_ollama_fallback_when_vllm_fails():
    """When vLLM is down or times out, seamlessly fall back to Ollama."""
    ollama_response = httpx.Response(
        200,
        json={"response": '{"product": "copper wire", "quantity": 100, "unit": "meter"}'},
        request=httpx.Request("POST", "http://localhost:11434/api/generate"),
    )

    async def mock_post_dispatcher(url, **kwargs):
        if "8001" in str(url):
            raise httpx.ConnectError("Connection refused on vLLM port 8001")
        return ollama_response

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=mock_post_dispatcher) as mock_post:
        result = await llm_client.generate_json(
            prompt="parse this with fallback",
            model="Qwen/Qwen3-8B",
            timeout=10.0,
        )
        assert result == {"product": "copper wire", "quantity": 100, "unit": "meter"}
        assert mock_post.call_count == 2
