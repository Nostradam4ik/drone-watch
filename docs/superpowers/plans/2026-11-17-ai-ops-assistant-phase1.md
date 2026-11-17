# AI Operator Assistant — Phase 1 (interactive RAG + tool-calling chat) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a working, read-only AI chat agent to Drone Watch: an operator asks a question in the dashboard, a
Mistral-backed tool-calling agent answers it using live Engine data and a Qdrant-backed semantic search over past
incidents, and the whole thing degrades to "cleanly off" if Mistral/Qdrant/the API key are unavailable.

**Architecture:** New `src/dronewatch/service/ai/` subpackage, isolated from `engine.py`: a thin Mistral HTTP client
(`llm.py`), a Qdrant-backed incident index (`rag.py`), read-only tool wrappers over `Engine`/`Runtime` (`tools.py`),
and a bounded tool-calling loop (`agent.py`). `app.py` wires this into `Runtime` behind one optional field and one
new endpoint; nothing else in the existing request path changes.

**Tech Stack:** Mistral AI REST API via `httpx` (no SDK), `qdrant-client` (official client, supports `:memory:` for
tests), existing FastAPI/pydantic/pytest stack.

**Spec:** [docs/superpowers/specs/2026-11-17-ai-ops-assistant-design.md](../specs/2026-11-17-ai-ops-assistant-design.md)

## Global Constraints

- LLM: Mistral `mistral-small-latest` for chat, `mistral-embed` for embeddings — called via raw `httpx`, no
  `mistralai` SDK dependency.
- Key only via `MISTRAL_API_KEY` environment variable — never in YAML, never logged, never echoed in a response.
- `ai.enabled: true` with no reachable key/LLM/Qdrant must **not** stop the service from starting or from tracking
  drones; it only makes `/api/ai/ask` answer with a clear "unavailable" response.
- No real network call to Mistral or Qdrant in `pytest` or in CI's `test`/`docker` jobs. The `e2e` CI job runs with
  no `MISTRAL_API_KEY` secret set, so the feature must degrade to off there without any special-casing of CI.
- `POST /api/ai/ask` sits behind `require_user`, exactly like `/api/state`, `/api/sensors`, `/api/incidents`.
- RAG indexing of a new incident happens independently of whether an LLM narration call for it succeeds (design
  doc, "Коли впаде Mistral"). This plan wires indexing itself into the live alert flow (Task 7) — otherwise
  `search_similar_incidents` would have nothing to ever find, since nothing else populates Qdrant in Phase 1. Only
  the LLM-written narration text (Phase 2) is deferred; the index-on-alert hook is not.
- Qdrant collection is created lazily (first real `index`/`search` call), never at `lifespan` startup.
- Phase 2 (auto-narrator) is out of scope for this plan but must be able to reuse `llm.py`, `rag.py`'s
  `IncidentIndex`, and `tools.py` unchanged — don't couple any of those three to the HTTP layer or to
  `/api/ai/ask` specifically.

## Review Focus

- Unauthenticated request to `POST /api/ai/ask` must be rejected the same way other protected endpoints reject it
  (401/403 via `require_user`), not accidentally left open because it's a new route. → Task 6.
- Mistral or Qdrant becoming unreachable **after** the service started with a valid-looking config (not just "no
  key at boot") must turn into a 503 with a clear message, never a 500 or a hung request. → Task 6.
- Tool output (sensor ids, incident text, zone names) must never be concatenated into the system/user prompt as
  free text the model could read as instructions — it must always arrive as a separate `tool`-role message, and the
  system prompt must say explicitly to treat it as data. → Task 5.
- A too-long or empty `question` must be rejected by validation (422), not sent to Mistral and billed. → Task 6.
- The agent must terminate in bounded time even if the model never stops requesting tools (`max_tool_iterations`
  reached) — it must return a usable, honest answer, not hang or crash. → Task 5.

---

## Task 1: Dependencies and `AiCfg` configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/dronewatch/service/config.py`
- Test: `tests/test_models.py` (or a new `tests/test_ai_config.py` if `test_models.py` doesn't already cover `config.py` — check before choosing)

**Interfaces:**
- Produces: `config.AiCfg` pydantic model with fields `enabled: bool = False`, `model: str = "mistral-small-latest"`,
  `embedding_model: str = "mistral-embed"`, `qdrant_url: str = "http://localhost:6333"`,
  `max_tool_iterations: int = Field(default=4, ge=1, le=10)`. Added to `Config` as `ai: AiCfg = AiCfg()`.

- [ ] **Step 1: Move `httpx` from `dev` extras to main `dependencies`, and add `qdrant-client>=1.9` to main `dependencies` in `pyproject.toml`**

- [ ] **Step 2: Write the failing test for config parsing**

```python
def test_ai_config_defaults_and_overrides():
    cfg = config.Config(mode="demo")
    assert cfg.ai.enabled is False and cfg.ai.model == "mistral-small-latest"
    cfg2 = config.Config(mode="demo", ai={"enabled": True, "max_tool_iterations": 2})
    assert cfg2.ai.enabled is True and cfg2.ai.max_tool_iterations == 2

def test_ai_config_rejects_out_of_range_iterations():
    with pytest.raises(ValidationError):
        config.Config(mode="demo", ai={"max_tool_iterations": 0})
```

- [ ] **Step 3: Run tests to verify they fail** (`AiCfg`/`ai` field don't exist yet)

Run: `pytest tests/test_models.py -k ai_config -v`
Expected: FAIL with `AttributeError` or `ValidationError` on unknown field `ai`

- [ ] **Step 4: Add `AiCfg` class and `ai: AiCfg = AiCfg()` field to `Config` in `config.py`**, following the existing
style of `MqttCfg`/`AuthCfg` (plain pydantic `BaseModel`, no custom validators needed for v1).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_models.py -k ai_config -v`
Expected: PASS

- [ ] **Step 6: `pip install -e ".[dev]"` to pick up the new dependencies, then run the full test suite once to confirm nothing else broke**

Run: `pytest -q`
Expected: all existing tests still PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/dronewatch/service/config.py tests/
git commit -m "ai: AiCfg config section and httpx/qdrant-client dependencies"
```

---

## Task 2: Mistral HTTP client (`llm.py`)

**Files:**
- Create: `src/dronewatch/service/ai/__init__.py` (empty)
- Create: `src/dronewatch/service/ai/errors.py`
- Create: `src/dronewatch/service/ai/llm.py`
- Test: `tests/test_ai_llm.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `errors.AiUnavailable(Exception)` — raised by every AI-layer failure (network error, non-2xx, timeout,
    malformed response). This is the one exception type the rest of the AI layer and `app.py` catch.
  - `llm.ToolCall` — `@dataclass(frozen=True, slots=True)`: `id: str`, `name: str`, `args: dict[str, Any]`.
  - `llm.ChatResult` — `@dataclass(frozen=True, slots=True)`: `content: str | None`, `tool_calls: list[ToolCall]`.
  - `llm.MistralClient.__init__(self, api_key: str, *, model: str, embedding_model: str, http: httpx.Client | None = None, timeout: float = 20.0)`
    — raises `ValueError` if `api_key` is falsy. `http` defaults to a real `httpx.Client(base_url="https://api.mistral.ai")`;
    tests inject one built with `httpx.MockTransport`.
  - `llm.MistralClient.chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResult`
    — POSTs to `/v1/chat/completions`, parses the first choice's message into a `ChatResult`. Any `httpx` exception
    or non-2xx status raises `AiUnavailable`.
  - `llm.MistralClient.embed(self, texts: list[str]) -> list[list[float]]`
    — POSTs to `/v1/embeddings`, returns one vector per input text in input order. Same error handling as `chat`.

- [ ] **Step 1: Write failing tests for `chat()`**

```python
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

def test_empty_api_key_rejected():
    with pytest.raises(ValueError):
        MistralClient("", model="m", embedding_model="e")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ai_llm.py -v`
Expected: FAIL (`ModuleNotFoundError: dronewatch.service.ai.llm`)

- [ ] **Step 3: Implement `errors.py` and `llm.py`** per the Interfaces block above. `arguments` in a tool call
arrives from Mistral as a JSON *string*; parse it with `json.loads`, and if that fails treat it as `AiUnavailable`
(malformed upstream response), not a crash.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ai_llm.py -v`
Expected: PASS

- [ ] **Step 5: Write and pass the `embed()` tests the same way** (success returns `list[list[float]]` in input
order; network/status errors raise `AiUnavailable`). Add to the same test file and implementation.

- [ ] **Step 6: `ruff check .` and `mypy`**

Run: `ruff check src/dronewatch/service/ai tests/test_ai_llm.py && mypy`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/dronewatch/service/ai/ tests/test_ai_llm.py
git commit -m "ai: Mistral HTTP client (chat + embeddings)"
```

---

## Task 3: Incident RAG index (`rag.py`)

**Files:**
- Create: `src/dronewatch/service/ai/rag.py`
- Test: `tests/test_ai_rag.py`

**Interfaces:**
- Consumes: `errors.AiUnavailable` (Task 2); an `embed: Callable[[list[str]], list[list[float]]]` callable shaped
  exactly like `MistralClient.embed` (tests pass a fake, production passes the bound method).
- Produces:
  - `rag.incident_text(row: dict[str, Any]) -> str` — exact format:
    `f"Інцидент: трек {row['track_id']}, зона {row['zone']}, рівень {row['level']}, "`
    `f"швидкість {row['speed']} м/с, підтверджено: {row['sensors']}, час {row['ts']}"` (`sensors` is a comma-joined
    string; works both for a stored `incidents` row turned into a `dict` and for a fresh `Alert` dict — see Task 7 —
    once its `sensors` list has been joined with `,`).
  - `rag.IncidentIndex.__init__(self, qdrant_url: str, embed: Callable[[list[str]], list[list[float]]], *, collection: str = "incidents", client: QdrantClient | None = None)`
    — `client` defaults to `QdrantClient(url=qdrant_url)`; tests pass `QdrantClient(location=":memory:")` directly.
  - `rag.IncidentIndex.index(self, point_id: str, row: dict[str, Any]) -> None` — `point_id` is the caller's
    responsibility (a SQLite incident id when one exists, a generated UUID otherwise — see Task 7); this keeps
    `IncidentIndex` independent of where the row came from. Ensures the collection exists (lazy, see Step 3), embeds
    `incident_text(row)`, upserts a point `id=point_id` with payload `{"text": ..., **row}`. Propagates
    `AiUnavailable` from `embed` (caller decides what to do).
  - `rag.IncidentIndex.search(self, query: str, k: int = 3) -> list[str]` — embeds `query`, returns the `text`
    payload field of the `k` nearest points, nearest first. Returns `[]` (not an error) if the collection doesn't
    exist yet (nothing indexed so far is a normal state, not a failure).

- [ ] **Step 1: Write failing tests**

```python
from qdrant_client import QdrantClient

def _fake_embed(dim: int):
    def embed(texts: list[str]) -> list[list[float]]:
        return [[float(hash(t) % 1000) / 1000] * dim for t in texts]
    return embed

def test_index_then_search_finds_it():
    idx = IncidentIndex("unused", _fake_embed(4), client=QdrantClient(location=":memory:"))
    idx.index("1", {"track_id": 7, "zone": "A", "level": "CRITICAL", "speed": 45.0, "sensors": "radar1,rf1", "ts": 1.0})
    hits = idx.search("трек 7 зона A", k=3)
    assert hits and "трек 7" in hits[0]

def test_search_on_empty_index_returns_empty_list():
    idx = IncidentIndex("unused", _fake_embed(4), client=QdrantClient(location=":memory:"))
    assert idx.search("щось") == []

def test_index_propagates_ai_unavailable_from_embed():
    def broken_embed(texts):
        raise AiUnavailable("down")
    idx = IncidentIndex("unused", broken_embed, client=QdrantClient(location=":memory:"))
    with pytest.raises(AiUnavailable):
        idx.index("1", {"track_id": 1, "zone": "A", "level": "WARNING", "speed": 1.0, "sensors": "radar1", "ts": 1.0})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ai_rag.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `rag.py`.** Collection dimensionality: don't hardcode Mistral's embedding size — on first
use, call `embed(["."])` once, measure `len(vector)`, and create the collection with that size and
`Distance.COSINE`; cache the created/verified state on the instance so repeated calls don't re-check. Wrap the
Qdrant client calls needed for "does the collection exist" so a fresh `:memory:` client and a fresh real server
both work the same way (`client.collection_exists(collection)`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ai_rag.py -v`
Expected: PASS

- [ ] **Step 5: `ruff check .` and `mypy`**

- [ ] **Step 6: Commit**

```bash
git add src/dronewatch/service/ai/rag.py tests/test_ai_rag.py
git commit -m "ai: Qdrant-backed incident index (lazy collection, cosine search)"
```

---

## Task 4: Read-only tools (`tools.py`) and incidents-query refactor

**Files:**
- Modify: `src/dronewatch/service/app.py` (extract `read_incidents`)
- Create: `src/dronewatch/service/ai/tools.py`
- Test: `tests/test_ai_tools.py`

**Interfaces:**
- Consumes: `Runtime` (from `app.py`, already exists: `engine`, `db_path`); `rag.IncidentIndex.search` (Task 3);
  `errors.AiUnavailable` (Task 2).
- Produces:
  - `app.read_incidents(db_path: str, limit: int = 100) -> list[dict[str, Any]]` — the exact query body currently
    inline in the `incidents()` handler, extracted so both the HTTP endpoint and the new tool call the same code.
  - `tools.TOOL_SCHEMAS: list[dict[str, Any]]` — Mistral/OpenAI-style function-calling schemas for exactly four
    tools: `get_state`, `get_sensors`, `get_incidents` (param `limit: integer`, default 10), `search_similar_incidents`
    (param `query: string`, required).
  - `tools.call_tool(name: str, args: dict[str, Any], *, rt: "Runtime", index: "IncidentIndex") -> dict[str, Any] | list[Any]`
    — dispatches by `name` to the four functions below; raises `KeyError` for an unknown tool name (the agent loop
    in Task 5 is responsible for turning that into a tool-result message, not a crash).
  - `tools.get_state(rt) -> dict[str, Any]` = `rt.engine.state()`.
  - `tools.get_sensors(rt) -> list[dict[str, Any]]` = `list(rt.engine.sensor_status().values())`.
  - `tools.get_incidents(rt, limit: int = 10) -> list[dict[str, Any]]` = `read_incidents(rt.db_path, limit)`.
  - `tools.search_similar_incidents(index, query: str, k: int = 3) -> list[str] | str` — returns `index.search(query, k)`;
    if that raises `AiUnavailable`, catches it and returns the string `"пошук схожих інцидентів тимчасово недоступний"`
    instead of propagating (Review Focus: Qdrant down must not abort the whole agent turn).

- [ ] **Step 1: Write the failing test for the refactor (no behavior change)**

```python
def test_read_incidents_matches_existing_query_shape(tmp_path):
    db_path = str(tmp_path / "incidents.db")
    eng = Engine(default_sensors(), [], db_path=db_path)
    eng.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (1,2,'WARNING','A',0,0,0,5,'radar1')")
    eng.db.commit()
    rows = app.read_incidents(db_path, limit=10)
    assert rows[0]["track_id"] == 2 and rows[0]["zone"] == "A"
```

- [ ] **Step 2: Run to verify it fails** (`app.read_incidents` doesn't exist)

- [ ] **Step 3: Extract `read_incidents` in `app.py`** and make the existing `incidents()` handler call it
(`return read_incidents(rt.db_path, limit)` after the existing `max(1, min(limit, 1000))` clamp, kept in
`read_incidents` itself so the clamp applies to tool calls too).

- [ ] **Step 4: Run to verify it passes, then run the full existing test suite to confirm `/api/incidents` behavior is unchanged**

Run: `pytest tests/test_ai_tools.py tests/test_health.py -v`
Expected: all PASS

- [ ] **Step 5: Write failing tests for the four tool functions and `call_tool` dispatch**

```python
class _FakeIndex:
    def __init__(self, hits=None, fail=False):
        self.hits, self.fail = hits or [], fail
    def search(self, query, k=3):
        if self.fail:
            raise AiUnavailable("down")
        return self.hits

def test_get_state_and_get_sensors_reflect_engine(...):
    ...  # construct Runtime(engine=Engine(default_sensors(), [...])), assert dict shapes match rt.engine.state()/sensor_status()

def test_search_similar_incidents_returns_hits():
    assert tools.search_similar_incidents(_FakeIndex(hits=["x"]), "q") == ["x"]

def test_search_similar_incidents_degrades_on_ai_unavailable():
    assert "недоступний" in tools.search_similar_incidents(_FakeIndex(fail=True), "q")

def test_call_tool_dispatches_by_name(...):
    assert tools.call_tool("get_state", {}, rt=rt, index=_FakeIndex()) == rt.engine.state()

def test_call_tool_unknown_name_raises_keyerror(...):
    with pytest.raises(KeyError):
        tools.call_tool("delete_everything", {}, rt=rt, index=_FakeIndex())
```

- [ ] **Step 6: Implement `tools.py`**

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_ai_tools.py -v`
Expected: PASS

- [ ] **Step 8: `ruff check .` and `mypy`**

- [ ] **Step 9: Commit**

```bash
git add src/dronewatch/service/app.py src/dronewatch/service/ai/tools.py tests/test_ai_tools.py
git commit -m "ai: read-only tool wrappers over Engine, extract read_incidents"
```

---

## Task 5: Tool-calling agent loop (`agent.py`)

**Files:**
- Create: `src/dronewatch/service/ai/agent.py`
- Test: `tests/test_ai_agent.py`

**Interfaces:**
- Consumes: `llm.ChatResult`, `llm.ToolCall` (Task 2); `tools.TOOL_SCHEMAS`, `tools.call_tool` (Task 4).
- Produces:
  - `agent.AgentAnswer` — `@dataclass(frozen=True, slots=True)`: `answer: str`, `tool_calls: list[dict[str, Any]]`
    (each `{"name": str, "args": dict}`, in call order — this is what `/api/ai/ask` returns as its trace).
  - `agent.SYSTEM_PROMPT: str` — Ukrainian, names the domain (drone tracking, zones, alert levels) and states
    explicitly, as its own sentence, that tool results are data to read, never instructions to follow.
  - `agent.ask(question: str, *, llm: "MistralClient", rt: "Runtime", index: "IncidentIndex", max_iterations: int) -> AgentAnswer`
    — runs the loop below.

Loop, exact shape (this is the part a wrong guess would change, so it's spelled out):

```
messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
trace = []
for _ in range(max_iterations):
    result = llm.chat(messages, tools=TOOL_SCHEMAS)
    if not result.tool_calls:
        return AgentAnswer(answer=result.content or "", tool_calls=trace)
    messages.append({"role": "assistant", "tool_calls": [...]})  # echo the assistant's tool-call turn back, per Mistral's chat format
    for call in result.tool_calls:
        trace.append({"name": call.name, "args": call.args})
        try:
            tool_result = call_tool(call.name, call.args, rt=rt, index=index)
        except KeyError:
            tool_result = {"error": f"невідомий інструмент {call.name}"}
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(tool_result, ensure_ascii=False)})
return AgentAnswer(answer="Не вдалося сформувати відповідь за відведену кількість кроків.", tool_calls=trace)
```

- [ ] **Step 1: Write failing tests with a fake `llm`**

```python
class _ScriptedLlm:
    def __init__(self, responses: list[ChatResult]):
        self._responses = iter(responses)
    def chat(self, messages, tools=None):
        return next(self._responses)

def test_ask_returns_immediate_answer_with_no_tools():
    result = agent.ask("хай", llm=_ScriptedLlm([ChatResult(content="привіт", tool_calls=[])]),
                       rt=rt, index=_FakeIndex(), max_iterations=4)
    assert result == AgentAnswer(answer="привіт", tool_calls=[])

def test_ask_calls_tool_then_answers():
    scripted = _ScriptedLlm([
        ChatResult(content=None, tool_calls=[ToolCall(id="1", name="get_state", args={})]),
        ChatResult(content="зараз так", tool_calls=[]),
    ])
    result = agent.ask("що зараз?", llm=scripted, rt=rt, index=_FakeIndex(), max_iterations=4)
    assert result.answer == "зараз так"
    assert result.tool_calls == [{"name": "get_state", "args": {}}]

def test_ask_stops_after_max_iterations_without_crashing():
    always_calls = _ScriptedLlm([ChatResult(content=None, tool_calls=[ToolCall(id=str(i), name="get_state", args={})])
                                 for i in range(10)])
    result = agent.ask("...", llm=always_calls, rt=rt, index=_FakeIndex(), max_iterations=3)
    assert "не вдалося" in result.answer.lower()
    assert len(result.tool_calls) == 3

def test_ask_unknown_tool_name_becomes_tool_error_not_crash():
    scripted = _ScriptedLlm([
        ChatResult(content=None, tool_calls=[ToolCall(id="1", name="delete_everything", args={})]),
        ChatResult(content="ясно", tool_calls=[]),
    ])
    result = agent.ask("...", llm=scripted, rt=rt, index=_FakeIndex(), max_iterations=4)
    assert result.answer == "ясно"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ai_agent.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `agent.py`** per the loop shape above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ai_agent.py -v`
Expected: PASS

- [ ] **Step 5: Add one more test asserting tool results travel as a `role: "tool"` message, never folded into the
system or user message content** (Review Focus: prompt-injection surface). Inspect the `messages` list the fake
`llm.chat` received on its second call and assert the tool's output text appears only in a message with
`role == "tool"`.

- [ ] **Step 6: Run full test suite, `ruff check .`, `mypy`**

- [ ] **Step 7: Commit**

```bash
git add src/dronewatch/service/ai/agent.py tests/test_ai_agent.py
git commit -m "ai: bounded tool-calling agent loop"
```

---

## Task 6: Wire into the FastAPI app (`Runtime.ai`, `build_ai`, `POST /api/ai/ask`)

**Files:**
- Modify: `src/dronewatch/service/app.py`
- Test: `tests/test_ai_endpoint.py`

**Interfaces:**
- Consumes: `config.AiCfg` (Task 1); `llm.MistralClient`, `errors.AiUnavailable` (Task 2); `rag.IncidentIndex`
  (Task 3); `agent.ask`, `agent.AgentAnswer` (Task 5); `dashboard_auth.LoginLimiter` (existing, reused as-is).
- Produces:
  - `app.AiRuntime` — `@dataclass(frozen=True, slots=True)`: `llm: MistralClient`, `index: IncidentIndex`,
    `max_iterations: int`, `limiter: LoginLimiter`.
  - `app.build_ai(cfg: config.Config) -> AiRuntime | None` — reads `os.environ.get("MISTRAL_API_KEY")`; returns
    `None` and logs a warning (not an error, not a `SystemExit`) if `cfg.ai.enabled` is `False`, or the key is
    missing while `cfg.ai.enabled` is `True`. Otherwise constructs `MistralClient`, `IncidentIndex` (passing
    `llm.embed`), and a `LoginLimiter(max_failures=10, window_s=60.0)` for the ask-rate-limit, all wrapped in
    `AiRuntime`.
  - `Runtime.ai: AiRuntime | None = None` field added to the existing `Runtime` dataclass.
  - `build_runtime` calls `build_ai(cfg)` and passes it through to `Runtime(..., ai=build_ai(cfg))`.
  - `AskRequest(BaseModel)`: `question: str = Field(min_length=1, max_length=500)`.
  - `AskResponse(BaseModel)`: `answer: str`, `tool_calls: list[dict[str, Any]]`.
  - `POST /api/ai/ask` (`dependencies=[Depends(require_user)]`, `response_model=None`): body `AskRequest`. If
    `rt.ai is None` → `JSONResponse({"detail": "AI-асистент вимкнено або недоступний"}, status_code=503)`. Else
    checks `rt.ai.limiter.attempt(<session token or "anon">)`; non-zero wait → `HTTPException(429, "Забагато
    запитів до AI-асистента, спробуйте пізніше", headers={"Retry-After": str(math.ceil(wait))})` (same pattern as
    login). Otherwise calls `agent.ask(body.question, llm=rt.ai.llm, rt=rt, index=rt.ai.index, max_iterations=rt.ai.max_iterations)`
    inside `try/except AiUnavailable` → on exception, same 503 body as "disabled". On success, returns
    `AskResponse(answer=result.answer, tool_calls=result.tool_calls)`.
  - Rate-limit key: use the dashboard session token from `access.token(request)` (same helper `/ws` already uses
    via `DashboardAccess`), falling back to `"anon"` when auth is disabled (demo mode) — so the limiter never
    errors when there's no session.

- [ ] **Step 1: Write the failing tests**

```python
def test_ask_without_ai_configured_returns_503(client_without_ai_session):  # rt.ai is None
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code == 503

def test_ask_requires_login(client_with_ai_but_no_session):
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code in (401, 403)

def test_ask_too_long_question_rejected(logged_in_client_with_ai):
    r = client.post("/api/ai/ask", json={"question": "x" * 501})
    assert r.status_code == 422

def test_ask_happy_path_returns_answer_and_trace(logged_in_client_with_ai_stub_llm):
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code == 200
    body = r.json()
    assert "answer" in body and isinstance(body["tool_calls"], list)

def test_ask_mistral_outage_mid_request_returns_503(logged_in_client_with_ai_llm_that_raises):
    r = client.post("/api/ai/ask", json={"question": "що зараз?"})
    assert r.status_code == 503

def test_ask_rate_limited_after_many_requests(logged_in_client_with_ai_stub_llm):
    for _ in range(10):
        client.post("/api/ai/ask", json={"question": "що?"})
    r = client.post("/api/ai/ask", json={"question": "що?"})
    assert r.status_code == 429 and "Retry-After" in r.headers
```

Build these fixtures the way `test_health.py`/`test_dashboard.py` already build a `TestClient` with a hand-built
`Runtime`/`DashboardAccess` on `app.app.state` (see `test_health.py:138-186` for the pattern) — inject a stub
`AiRuntime` whose `llm` is a small fake object implementing `.chat`/`.embed`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ai_endpoint.py -v`
Expected: FAIL (route/field don't exist)

- [ ] **Step 3: Implement the `Runtime.ai` field, `build_ai`, `AskRequest`/`AskResponse`, and the route** in `app.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ai_endpoint.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite, `ruff check .`, `mypy`**

Run: `pytest -q && ruff check . && mypy`
Expected: all PASS, no lint/type errors

- [ ] **Step 6: Commit**

```bash
git add src/dronewatch/service/app.py tests/test_ai_endpoint.py
git commit -m "ai: wire chat agent into Runtime and POST /api/ai/ask"
```

---

## Task 7: Index new alerts into Qdrant as they happen

**Files:**
- Modify: `src/dronewatch/service/app.py` (`scan_step`)
- Test: `tests/test_ai_scan_indexing.py`

**Interfaces:**
- Consumes: `rag.IncidentIndex.index(point_id: str, row: dict[str, Any])` (Task 3, revised); `Runtime.ai` (Task 6);
  `errors.AiUnavailable` (Task 2). `rt.engine.tick()` already returns `list[Alert]` (`engine.py:236` via `_evaluate`)
  — `scan_step` currently discards that return value (`app.py:200`, bare `rt.engine.tick()`).
- Produces: no new public names — this task only changes `scan_step`'s body.

- [ ] **Step 1: Write the failing test**

```python
def test_scan_step_indexes_new_alerts_when_ai_enabled(monkeypatch):
    indexed = []
    class _RecordingIndex:
        def index(self, point_id, row):
            indexed.append((point_id, row))
    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 50, "WARNING")]),
                     ai=app.AiRuntime(llm=object(), index=_RecordingIndex(), max_iterations=4, limiter=LoginLimiter()))
    # feed one event that crosses into the zone so tick() produces a new alert, then run scan_step
    rt.engine.ingest([...])
    asyncio.run(app.scan_step(app.app, rt))
    assert indexed and indexed[0][1]["zone"] == "z"

def test_scan_step_skips_indexing_when_ai_disabled():
    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 50, "WARNING")]))  # ai=None by default
    rt.engine.ingest([...])
    asyncio.run(app.scan_step(app.app, rt))  # must not raise just because rt.ai is None

def test_scan_step_survives_qdrant_indexing_failure(monkeypatch):
    class _BrokenIndex:
        def index(self, point_id, row):
            raise AiUnavailable("down")
    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 50, "WARNING")]),
                     ai=app.AiRuntime(llm=object(), index=_BrokenIndex(), max_iterations=4, limiter=LoginLimiter()))
    rt.engine.ingest([...])
    asyncio.run(app.scan_step(app.app, rt))  # must not raise, must not stop the scan loop
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_ai_scan_indexing.py -v`
Expected: FAIL (indexing never called)

- [ ] **Step 3: Implement.** In `scan_step`, change `rt.engine.tick()` to `new_alerts = rt.engine.tick()`. After the
existing `payload`/`broadcast` logic, if `rt.ai is not None`, iterate `new_alerts` and call
`rt.ai.index.index(str(uuid.uuid4()), {**alert, "sensors": ",".join(alert["sensors"])})` for each, inside a
`try/except AiUnavailable: pass` (mirrors `scan_step`'s existing top-level `except Exception` pattern — AI indexing
failures must count as neither a scan error nor crash the loop; log once via the same throttled-logging style
already used for `sh.consecutive` if you want visibility, but don't let it touch `sh.errors`).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_ai_scan_indexing.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite once more** — this touches `scan_step`, shared by every existing scan-loop test.

Run: `pytest -q`
Expected: all PASS

- [ ] **Step 6: `ruff check .` and `mypy`**

- [ ] **Step 7: Commit**

```bash
git add src/dronewatch/service/app.py tests/test_ai_scan_indexing.py
git commit -m "ai: index new alerts into Qdrant as they occur"
```

---

## Task 8: Dashboard panel, docker-compose, demo config, README

**Files:**
- Modify: `src/dronewatch/service/static/index.html`
- Modify: `docker-compose.yml`
- Modify: `deploy/config.yaml`
- Modify: `src/dronewatch/service/config.example.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `POST /api/ai/ask` (Task 6) — `{"question": str} -> {"answer": str, "tool_calls": [...]}` or a non-200
  the UI must show as "AI недоступний" rather than a blank panel or a JS error.

- [ ] **Step 1: Add an "AI" panel to `index.html`**: a text input, a "Запитати" button, and a log area rendering
each exchange (question, answer, and the `tool_calls` names as a small inline trace — this is the visible "it's a
real agent, not a chatbot skin" cue for the demo). Reuse the existing `api()`-style fetch helper pattern already in
the file for the GET calls; add a small POST helper next to it. On non-200, render "AI недоступний" in the panel
instead of throwing.

- [ ] **Step 2: Add a `qdrant` service to `docker-compose.yml`**, profile `["ai"]` (opt-in, so the CI `e2e` job's
`--profile demo-sensors` run never starts it), image `qdrant/qdrant`, a named volume for storage, no published
port (internal network only, same posture as `mosquitto`). Add to the `service` block's `environment`:
`MISTRAL_API_KEY: ${MISTRAL_API_KEY:-}` (empty by default — never commit a real key) and
`QDRANT_URL: http://qdrant:6333`.

- [ ] **Step 3: Set `ai: {enabled: true}` in `deploy/config.yaml`** (the demo stand's config) — safe because
`build_ai` degrades to `None` with a log warning whenever `MISTRAL_API_KEY` isn't actually set, which is exactly
the CI `e2e` job's situation.

- [ ] **Step 4: Document the `ai:` block in `config.example.yaml`** with a comment per field, matching the existing
style (see `MqttCfg`'s documented fields for the tone).

- [ ] **Step 5: Validate the compose file**

Run: `docker compose config --quiet`
Expected: no error

- [ ] **Step 6: Add a README section "AI-асистент оператора (RAG + tool-calling)"** after "Потоп подій і
потокобезпека": what it does, how to enable it (`MISTRAL_API_KEY=... docker compose --profile demo-sensors
--profile ai up --build`), that it's entirely optional/read-only, and the threat-model line from the design doc
(tool output is data, never instructions; agent cannot write to the system). Add `ai/` to the "Структура
репозиторію" listing under `service/`.

- [ ] **Step 7: Add the demo-night note from the design doc's "Показ завтра" section to the README's run
instructions**: run the emulator for a few minutes before approaching anyone, so the first RAG question on the
show floor doesn't hit an empty index.

- [ ] **Step 8: Manual smoke test with a real key** (not automated — do this once before the show): `MISTRAL_API_KEY=<real key> docker compose --profile demo-sensors --profile ai up --build`, open the dashboard, let the emulator
run a few minutes, ask the AI panel a question about a zone, confirm an answer with a non-empty tool-call trace
appears. Then stop the stack, unset `MISTRAL_API_KEY`, bring it up again, and confirm the service still starts and
the AI panel shows "AI недоступний" instead of breaking the rest of the dashboard.

- [ ] **Step 9: Commit**

```bash
git add src/dronewatch/service/static/index.html docker-compose.yml deploy/config.yaml src/dronewatch/service/config.example.yaml README.md
git commit -m "ai: dashboard chat panel, Qdrant compose service, docs"
```
