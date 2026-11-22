"""POST /api/ai/ask: вхід обов'язковий, 503 коли AI вимкнено/недоступний, 422 на завелике питання, rate-limit."""
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import default_sensors
from dronewatch.service import app
from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.ai.llm import ChatResult
from dronewatch.service.dashboard_auth import SessionStore
from dronewatch.service.dashboard_web import DashboardAccess
from dronewatch.service.engine import Engine, Zone


class _StubLlm:
    def __init__(self, responses=None, raises=False):
        self._responses = iter(responses or [ChatResult(content="відповідь", tool_calls=[])])
        self.raises = raises

    def chat(self, messages, tools=None):
        if self.raises:
            raise AiUnavailable("down")
        return next(self._responses)

    def embed(self, texts):
        return [[0.0] for _ in texts]


def _client(*, ai, logged_in):
    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 100, "WARNING")]), ai=ai)
    app.app.state.runtime = rt
    sessions = SessionStore(ttl_s=3600)
    app.app.state.access = DashboardAccess(sessions=sessions)
    client = TestClient(app.app, raise_server_exceptions=False)
    if logged_in:
        token = sessions.create("operator")
        client.cookies.set("dw_session", token)
    return client


def _ai_runtime(llm):
    from qdrant_client import QdrantClient

    from dronewatch.service.ai.rag import IncidentIndex
    from dronewatch.service.app import AiRuntime
    from dronewatch.service.dashboard_auth import LoginLimiter
    return AiRuntime(llm=llm, index=IncidentIndex("unused", llm.embed, client=QdrantClient(location=":memory:")),
                     max_iterations=4, limiter=LoginLimiter(max_failures=10, window_s=60.0))


def test_ask_without_ai_configured_returns_503():
    client = _client(ai=None, logged_in=True)
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code == 503


def test_ask_requires_login():
    client = _client(ai=_ai_runtime(_StubLlm()), logged_in=False)
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code in (401, 403)


def test_ask_too_long_question_rejected():
    client = _client(ai=_ai_runtime(_StubLlm()), logged_in=True)
    r = client.post("/api/ai/ask", json={"question": "x" * 501})
    assert r.status_code == 422


def test_ask_happy_path_returns_answer_and_trace():
    client = _client(ai=_ai_runtime(_StubLlm()), logged_in=True)
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "відповідь" and isinstance(body["tool_calls"], list)


def test_ask_mistral_outage_mid_request_returns_503():
    client = _client(ai=_ai_runtime(_StubLlm(raises=True)), logged_in=True)
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code == 503


def test_ask_mistral_outage_is_logged(caplog):
    """Фінальний рев'ю: раніше AiUnavailable у /api/ai/ask ковтався мовчки — на живому показі збій ключа/мережі
    був би не діагностований жодним рядком у логах, лише непояснюваним "AI недоступний" для оператора."""
    client = _client(ai=_ai_runtime(_StubLlm(raises=True)), logged_in=True)
    with caplog.at_level("WARNING", logger="dronewatch"):
        client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert any("ai" in r.message.lower() or "down" in r.message.lower() for r in caplog.records)


def test_ask_rate_limited_after_many_requests():
    client = _client(ai=_ai_runtime(_StubLlm(responses=[ChatResult(content="ок", tool_calls=[]) for _ in range(20)])),
                     logged_in=True)
    for _ in range(10):
        client.post("/api/ai/ask", json={"question": "що?"})
    r = client.post("/api/ai/ask", json={"question": "що?"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_ask_rate_limit_cannot_be_bypassed_by_rotating_the_session_cookie():
    """Коли вхід вимкнено (demo), сире значення кукі ще не перевірене — якщо ключ ліміту береться прямо з нього,
    клієнт обходить ліміт, просто відправляючи щоразу нове значення cookie. Ключ має братись з user (require_user),
    тобто з уже перевіреної автентифікації, а не з неперевіреного заголовка клієнта."""
    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 100, "WARNING")]),
                     ai=_ai_runtime(_StubLlm(responses=[ChatResult(content="ок", tool_calls=[]) for _ in range(20)])))
    app.app.state.runtime = rt
    app.app.state.access = DashboardAccess()   # вхід вимкнено: будь-яке значення dw_session "проходить" як анонім
    client = TestClient(app.app, raise_server_exceptions=False)
    statuses = []
    for i in range(15):
        client.cookies.set("dw_session", f"forged-{i}")
        statuses.append(client.post("/api/ai/ask", json={"question": "що?"}).status_code)
    assert 429 in statuses, "обертання кукі не повинно обходити ліміт запитів до AI"
