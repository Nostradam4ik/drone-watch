"""Клієнт Mistral (chat + embeddings): парсинг відповіді, tool-calls, деградація на AiUnavailable."""
import httpx
import pytest

from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.ai.llm import ChatResult, MistralClient, ToolCall


def _client(handler) -> MistralClient:
    return MistralClient("test-key", model="mistral-small-latest", embedding_model="mistral-embed",
                          http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.mistral.ai"))


def test_chat_returns_final_answer_when_no_tool_calls():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Привіт", "tool_calls": None}}]})
    result = _client(handler).chat([{"role": "user", "content": "hi"}])
    assert result == ChatResult(content="Привіт", tool_calls=[])


def test_chat_parses_tool_calls():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "function": {"name": "get_state", "arguments": "{}"}}]}}]})
    result = _client(handler).chat([{"role": "user", "content": "hi"}], tools=[])
    assert result.tool_calls == [ToolCall(id="call_1", name="get_state", args={})]


def test_chat_raises_ai_unavailable_on_network_error():
    def handler(request):
        raise httpx.ConnectError("boom")
    with pytest.raises(AiUnavailable):
        _client(handler).chat([{"role": "user", "content": "hi"}])


def test_chat_raises_ai_unavailable_on_non_2xx():
    def handler(request):
        return httpx.Response(401, json={"message": "bad key"})
    with pytest.raises(AiUnavailable):
        _client(handler).chat([{"role": "user", "content": "hi"}])


def test_chat_raises_ai_unavailable_on_malformed_tool_arguments():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "function": {"name": "get_state", "arguments": "{not json"}}]}}]})
    with pytest.raises(AiUnavailable):
        _client(handler).chat([{"role": "user", "content": "hi"}], tools=[])


def test_empty_api_key_rejected():
    with pytest.raises(ValueError):
        MistralClient("", model="m", embedding_model="e")


def test_embed_returns_vectors_in_input_order():
    def handler(request):
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0]}, {"embedding": [3.0, 4.0]}]})
    result = _client(handler).embed(["a", "b"])
    assert result == [[1.0, 2.0], [3.0, 4.0]]


def test_embed_raises_ai_unavailable_on_network_error():
    def handler(request):
        raise httpx.ConnectError("boom")
    with pytest.raises(AiUnavailable):
        _client(handler).embed(["a"])


def test_embed_raises_ai_unavailable_on_non_2xx():
    def handler(request):
        return httpx.Response(500, json={"message": "down"})
    with pytest.raises(AiUnavailable):
        _client(handler).embed(["a"])
