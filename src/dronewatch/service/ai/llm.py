"""Тонкий клієнт Mistral AI: chat completions (з tool-calling) і embeddings. Прямі HTTP-виклики через httpx,
без офіційного SDK — менше залежностей, легше підмінити в тестах (httpx.MockTransport).

Будь-яка мережева помилка чи не-2xx відповідь перетворюється на AiUnavailable: виклик, що чекав на AI, сам
вирішує, що робити (503 оператору, пропустити наратив тощо), а не падає з необробленим винятком httpx."""
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from dronewatch.service.ai.errors import AiUnavailable

API_BASE = "https://api.mistral.ai"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class MistralClient:
    def __init__(self, api_key: str, *, model: str, embedding_model: str, http: httpx.Client | None = None,
                 timeout: float = 20.0) -> None:
        if not api_key:
            raise ValueError("MISTRAL_API_KEY порожній")
        self.model, self.embedding_model = model, embedding_model
        self._http = http or httpx.Client(base_url=API_BASE, timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(path, json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise AiUnavailable(f"Mistral {path}: мережева помилка: {e}") from e
        if response.status_code >= 300:
            raise AiUnavailable(f"Mistral {path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as e:
            raise AiUnavailable(f"Mistral {path}: відповідь не є JSON") from e

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResult:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools is not None:
            body["tools"] = tools
        data = self._post("/v1/chat/completions", body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise AiUnavailable("Mistral chat: неочікувана форма відповіді") from e
        raw_calls = message.get("tool_calls") or []
        calls = []
        for c in raw_calls:
            try:
                args = json.loads(c["function"]["arguments"])
                calls.append(ToolCall(id=c["id"], name=c["function"]["name"], args=args))
            except (KeyError, TypeError, json.JSONDecodeError) as e:
                raise AiUnavailable("Mistral chat: не вдалося розібрати tool_call") from e
        return ChatResult(content=message.get("content"), tool_calls=calls)

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/v1/embeddings", {"model": self.embedding_model, "input": texts})
        try:
            return [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError) as e:
            raise AiUnavailable("Mistral embeddings: неочікувана форма відповіді") from e
