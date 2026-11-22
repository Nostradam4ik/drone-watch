"""Ролі на кожному маршруті й журнал дій для кожної дії (вхід, вихід, відмова, розкладка, рішення, читання журналу; перегляд покриття не пишеться)."""
import asyncio
from unittest.mock import Mock

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from dronewatch.service import app, config
from dronewatch.service.audit import AuditError
from dronewatch.service.dashboard_auth import LoginLimiter, SessionStore, UserEntry, Users, has_role
from dronewatch.service.dashboard_web import ANONYMOUS, COOKIE, DashboardAccess
from helpers_auth import login_as
from test_dashboard import FAST, PASSWORD

LAYOUT = {"sensors": [{"id": "r1", "type": "radar-mid", "x": 0, "y": 0}]}
ROUTE_ALLOWLIST = {("GET", "/login"), ("POST", "/api/login"), ("POST", "/api/logout"), ("POST", "/api/events"), ("GET", "/api/health")}


DOCS_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}   # службові сторінки FastAPI (Starlette Route)


PAGE_ONLY_ROUTES = {("GET", "/")}   # лише тут дозволена page_identity (dw_page_only): вона сама нічого не забороняє


def has_marker(dependant, marker) -> bool:
    return any(getattr(d.call, marker, None) is not None or has_marker(d, marker) for d in dependant.dependencies)


def has_role_dependency(dependant) -> bool:
    return has_marker(dependant, "dw_min_role")


def guard_violations(routes):
    """Маршрути без require_role (поза allowlist), а також page_identity будь-де, крім GET /."""
    out = []
    for m, path, dependant in routes:
        if path.startswith("/static/") or m in ("HEAD", "OPTIONS") or (m, path) in ROUTE_ALLOWLIST:
            continue
        page_only = has_marker(dependant, "dw_page_only")
        if page_only and (m, path) not in PAGE_ONLY_ROUTES:
            out.append((m, path))   # позначка сторінки на API-маршруті: guard не має вважати це перевіркою ролі
        elif not page_only and not has_role_dependency(dependant):
            out.append((m, path))
    return out


def http_routes(application=None):
    """(метод, шлях, dependant) для кожного HTTP-маршруту, зокрема з include_router. FastAPI 0.16x не розгортає підключений
    роутер у app.routes (там лежить один `_IncludedRouter`), тож наївний обхід лише APIRoute пропустив би /api/me, /api/login
    тощо й guard проходив би порожньо. Невідомий тип маршруту валить тест: краще впасти, ніж мовчки нічого не перевірити."""
    out = []
    for r in (application or app.app).routes:
        if isinstance(r, APIRoute):
            out += [(m, r.path, r.dependant) for m in r.methods]
        elif hasattr(r, "effective_route_contexts"):
            for ctx in r.effective_route_contexts():
                assert ctx.methods, f"WebSocket у підключеному роутері ({ctx.path}): перевірте роль вручну й додайте сюди"
                out += [(m, ctx.path, ctx.dependant) for m in ctx.methods]
        elif isinstance(r, APIWebSocketRoute) and r.path == "/ws":
            continue   # WebSocket не має require_role: роль перевіряє сам обробник (test_ws_*)
        elif isinstance(r, Route) and r.path in DOCS_ROUTES:
            continue
        else:
            raise AssertionError(f"невідомий маршрут {r!r}: guard не вміє його перевірити")
    return out


def _demo_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    rt = app.build_runtime(config.Config(mode="demo"))
    rt.engine.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) "
                         "VALUES (1, 1, 'WARNING', 'A', 0, 0, 0, 5, 'radar1')")
    rt.engine.db.commit()
    app.app.state.runtime = rt
    return rt


@pytest.fixture
def demo_client(tmp_path, monkeypatch):
    _demo_runtime(tmp_path, monkeypatch)
    app.app.state.access = DashboardAccess(sessions=SessionStore(3600), limiter=LoginLimiter())
    return TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    _demo_runtime(tmp_path, monkeypatch)
    app.app.state.access = DashboardAccess()
    return TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def login_client(tmp_path, monkeypatch):
    _demo_runtime(tmp_path, monkeypatch)
    app.app.state.access = DashboardAccess(Users({"alice": UserEntry(FAST, "admin")}), SessionStore(3600),
                                           LoginLimiter(max_failures=5, window_s=300))
    return TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)


def _audit(c):
    return c.app.state.runtime.engine.audit


def _actions(c):
    return [r["action"] for r in reversed(_audit(c).read(1000))]   # від найстарішого до найновішого


# ---------- guard: жоден маршрут без ролі ----------
def test_every_route_declares_a_role():
    assert guard_violations(http_routes()) == []


def test_page_identity_on_an_api_route_fails_the_guard():
    """page_identity нічого не забороняє: якщо її поставити на API-маршрут, guard має впасти, а не вважати маршрут захищеним."""
    from typing import Annotated

    from fastapi import Depends, FastAPI

    from dronewatch.service.dashboard_web import Identity, page_identity, require_role
    probe = FastAPI()

    @probe.get("/")
    def page(ident: Annotated[Identity | None, Depends(page_identity)]) -> dict:
        return {}

    @probe.get("/api/leak")
    def leak(ident: Annotated[Identity | None, Depends(page_identity)]) -> dict:
        return {}

    @probe.post("/api/leak2")
    def leak2(ident: Annotated[Identity | None, Depends(page_identity)],
              _: Annotated[Identity, Depends(require_role("viewer"))]) -> dict:   # навіть разом із роллю: позначка лише для GET /
        return {}

    assert guard_violations(http_routes(probe)) == [("GET", "/api/leak"), ("POST", "/api/leak2")]


def test_guard_sees_router_routes_and_allowlist_is_alive():
    """Guard справді бачить маршрути з dashboard_web.router, і в allowlist немає мертвих записів."""
    present = {(m, path) for m, path, _ in http_routes()}
    assert present >= ROUTE_ALLOWLIST
    assert ("GET", "/api/me") in present and ("GET", "/api/audit") in present


# ---------- матриця роль × ендпоінт ----------
MATRIX = [  # (method, path, body, мінімальна роль)
    ("GET", "/", None, "viewer"),
    ("GET", "/api/state", None, "viewer"), ("GET", "/api/incidents", None, "viewer"),
    ("GET", "/api/config", None, "viewer"), ("GET", "/api/sensors", None, "viewer"),
    ("GET", "/api/coverage", None, "viewer"), ("GET", "/api/me", None, "viewer"),
    ("POST", "/api/incidents/1/review", {"status": "confirmed"}, "operator"),
    ("POST", "/api/ai/ask", {"question": "?"}, "operator"),
    ("PUT", "/api/layout", LAYOUT, "admin"), ("DELETE", "/api/layout", None, "admin"),
    ("POST", "/api/coverage", LAYOUT, "admin"), ("GET", "/api/audit", None, "admin"),
]


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
@pytest.mark.parametrize(("method", "path", "body", "need"), MATRIX)
def test_role_matrix(demo_client, role, method, path, body, need):
    login_as(demo_client, role)
    r = demo_client.request(method, path, json=body)
    if has_role(role, need):
        assert r.status_code not in (401, 403, 303), r.text
    elif path == "/":
        assert r.status_code == 303   # сторінка не дає 403, а веде на вхід
    else:
        assert r.status_code == 403
        assert r.json()["detail"] == "Недостатньо прав"
        row = _audit(demo_client).read()[0]
        assert (row["action"], row["result"], row["user"], row["role"]) == ("denied", "denied", "alice", role)
        assert row["target"] == f"{method} {path} needs {need}"


@pytest.mark.parametrize(("method", "path", "body", "need"), [m for m in MATRIX if m[1] != "/"])
def test_no_session_gives_401(demo_client, method, path, body, need):
    assert demo_client.request(method, path, json=body).status_code == 401


def test_anonymous_is_viewer_when_auth_is_off(anon_client):
    me = anon_client.get("/api/me").json()
    assert me == {"user": ANONYMOUS, "role": "viewer", "auth": False}
    assert anon_client.get("/").status_code == 200
    for path in ("/api/state", "/api/config", "/api/sensors", "/api/incidents", "/api/coverage"):
        assert anon_client.get(path).status_code == 200, path
    assert anon_client.put("/api/layout", json=LAYOUT).status_code == 403
    assert anon_client.delete("/api/layout").status_code == 403
    assert anon_client.post("/api/coverage", json=LAYOUT).status_code == 403
    assert anon_client.post("/api/incidents/1/review", json={"status": "confirmed"}).status_code == 403
    assert anon_client.post("/api/ai/ask", json={"question": "?"}).status_code == 403
    assert anon_client.get("/api/audit").status_code == 403
    assert _audit(anon_client).read()[0]["user"] == ANONYMOUS


def test_unknown_session_role_is_denied(demo_client):
    token = demo_client.app.state.access.sessions.create("alice", "root")  # type: ignore[arg-type]
    demo_client.cookies.set(COOKIE, token)
    assert demo_client.get("/api/state").status_code == 403
    assert _audit(demo_client).read()[0]["action"] == "denied"
    assert demo_client.get("/").status_code == 303
    assert demo_client.get("/login").status_code == 200, "сесія без чинної ролі не має зациклювати / ↔ /login"
    assert set(demo_client.get("/api/health").json()) == {"ok"}, "невідома роль не бачить подробиць health"


def test_denied_is_still_403_when_audit_fails(demo_client, monkeypatch):
    login_as(demo_client, "viewer")
    monkeypatch.setattr(_audit(demo_client), "append", Mock(side_effect=AuditError("ro")))
    assert demo_client.put("/api/layout", json=LAYOUT).status_code == 403


def test_denied_rows_are_aggregated_but_every_denial_is_logged(demo_client, caplog):
    """Флуд заборонених запитів не роздуває журнал: один `denied` на адресу й користувача за вікно; у лозі кожна відмова."""
    login_as(demo_client, "viewer", user="vera")
    with caplog.at_level("WARNING", logger="dronewatch.dashboard"):
        for _ in range(50):
            assert demo_client.delete("/api/layout").status_code == 403
        assert demo_client.get("/api/audit").status_code == 403
    rows = _audit(demo_client).read(1000)
    assert [(r["action"], r["target"]) for r in rows] == [("denied", "DELETE /api/layout needs admin")]
    assert sum("відмова" in r.getMessage() for r in caplog.records) == 51
    login_as(demo_client, "viewer", user="otto")   # інший користувач з тієї ж адреси: окремий запис
    demo_client.delete("/api/layout")
    assert [r["user"] for r in _audit(demo_client).read()] == ["otto", "vera"]


def test_denied_row_again_after_the_window(demo_client, monkeypatch):
    from dronewatch.service import dashboard_web
    monkeypatch.setattr(dashboard_web, "DENIED_WINDOW_S", 0.0)
    login_as(demo_client, "viewer")
    demo_client.delete("/api/layout")
    demo_client.delete("/api/layout")
    assert len(_audit(demo_client).read()) == 2


def test_role_check_comes_before_body_validation(demo_client):
    """Viewer не має дізнаватися нічого про вимоги до тіла admin-ендпоінта (і не отримувати 422 замість 403)."""
    login_as(demo_client, "viewer")
    assert demo_client.put("/api/layout", json={"bogus": 1}).status_code == 403


# ---------- вхід і вихід ----------
def test_login_success_failure_logout_are_audited(login_client):
    c = login_client
    assert c.post("/api/login", json={"username": "alice", "password": "wrong-password-123"}).status_code == 401
    assert c.post("/api/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert c.get("/api/me").json() == {"user": "alice", "role": "admin", "auth": True}
    assert c.post("/api/logout", json={}).status_code == 200
    rows = list(reversed(_audit(c).read()))
    assert [(r["action"], r["result"]) for r in rows] == [("login_failed", "failed"), ("login", "ok"), ("logout", "ok")]
    assert rows[1]["role"] == "admin" and rows[2]["user"] == "alice" and rows[2]["role"] == "admin"
    assert all(r["ip"] for r in rows)
    assert all(PASSWORD not in str(r) and "wrong-password-123" not in str(r) for r in rows), "паролів у журналі немає"


def test_blocked_logins_are_aggregated(login_client):
    for _ in range(20):
        login_client.post("/api/login", json={"username": "alice", "password": "wrong-password-123"})
    actions = _actions(login_client)
    assert actions.count("login_failed") == 5, "не більше, ніж дозволяє LoginLimiter"
    assert actions.count("login_blocked") == 1
    row = next(r for r in _audit(login_client).read() if r["action"] == "login_blocked")
    assert (row["user"], row["target"], row["result"]) == ("alice", "", "denied")


def test_audit_failure_blocks_login(login_client, monkeypatch):
    monkeypatch.setattr(_audit(login_client), "append", Mock(side_effect=AuditError("ro")))
    r = login_client.post("/api/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 503 and r.json()["detail"] == "Журнал дій недоступний"
    assert "set-cookie" not in r.headers
    assert login_client.get("/api/me").status_code == 401


def test_login_without_runtime_fails_closed(login_client):
    del login_client.app.state.runtime
    r = login_client.post("/api/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 503 and "set-cookie" not in r.headers


def test_failed_login_audit_failure_keeps_401(login_client, monkeypatch):
    monkeypatch.setattr(_audit(login_client), "append", Mock(side_effect=AuditError("ro")))
    assert login_client.post("/api/login", json={"username": "alice", "password": "wrong-password-123"}).status_code == 401


def test_logout_works_even_if_audit_fails(login_client, monkeypatch):
    assert login_client.post("/api/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    token = login_client.cookies.get(COOKIE)
    monkeypatch.setattr(_audit(login_client), "append", Mock(side_effect=AuditError("ro")))
    assert login_client.post("/api/logout", json={}).status_code == 200
    assert login_client.app.state.access.sessions.get(token) is None, "сесію відкликано, хоч журнал і впав"


# ---------- розкладка, покриття ----------
def test_layout_change_writes_started_then_ok(demo_client):
    login_as(demo_client, "admin")
    assert demo_client.put("/api/layout", json=LAYOUT).status_code == 200
    assert demo_client.delete("/api/layout").status_code == 200
    rows = [(r["action"], r["result"], r["target"]) for r in reversed(_audit(demo_client).read())]
    assert rows == [("layout_save", "started", "sensors=1"), ("layout_save", "ok", "sensors=1"),
                    ("layout_reset", "started", "reset"), ("layout_reset", "ok", "reset")]


def test_layout_conflict_writes_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    rt = app.build_runtime(config.Config(mode="demo"))
    rt.demo_src = None   # як у бойовому режимі: розкладку не змінюють
    app.app.state.runtime = rt
    app.app.state.access = DashboardAccess(sessions=SessionStore(3600))
    c = TestClient(app.app, raise_server_exceptions=False)
    login_as(c, "admin")
    assert c.put("/api/layout", json=LAYOUT).status_code == 409
    assert [(r["action"], r["result"]) for r in reversed(rt.engine.audit.read())] == [("layout_save", "started"), ("layout_save", "error")]


def test_audit_failure_blocks_layout_change(demo_client, monkeypatch):
    login_as(demo_client, "admin")
    before = demo_client.get("/api/config").json()["sensors"]
    monkeypatch.setattr(demo_client.app.state.runtime.engine.audit, "append", Mock(side_effect=AuditError("ro")))
    r = demo_client.put("/api/layout", json=LAYOUT)
    assert r.status_code == 503 and r.json()["detail"] == "Журнал дій недоступний"
    assert demo_client.delete("/api/layout").status_code == 503
    assert demo_client.get("/api/config").json()["sensors"] == before


def test_coverage_preview_is_admin_only_but_not_audited(demo_client, monkeypatch):
    """POST /api/coverage стану не змінює, а редактор кличе його щокілька сотень мс: у журнал не пишемо (і збій журналу
    його не блокує). Право лишається admin (див. матрицю)."""
    login_as(demo_client, "admin")
    assert demo_client.post("/api/coverage", json=LAYOUT).status_code == 200
    assert _audit(demo_client).read() == []
    append = Mock(side_effect=AuditError("ro"))
    monkeypatch.setattr(_audit(demo_client), "append", append)
    assert demo_client.post("/api/coverage", json=LAYOUT).status_code == 200
    append.assert_not_called()


# ---------- рішення по тривозі ----------
def test_review_is_audited_in_the_same_step(demo_client):
    login_as(demo_client, "operator", user="olga")
    assert demo_client.post("/api/incidents/1/review", json={"status": "confirmed"}).status_code == 200
    row = _audit(demo_client).read()[0]
    assert (row["user"], row["role"], row["action"], row["target"], row["result"]) == (
        "olga", "operator", "review", "incident=1 status=confirmed", "ok")


def test_review_audit_failure_gives_503_and_changes_nothing(demo_client, monkeypatch):
    login_as(demo_client, "operator")
    eng = demo_client.app.state.runtime.engine

    def boom(entry):
        raise RuntimeError("disk full")
    monkeypatch.setattr(eng.audit, "append_locked", boom)
    r = demo_client.post("/api/incidents/1/review", json={"status": "confirmed"})
    assert r.status_code == 503 and r.json()["detail"] == "Журнал дій недоступний"
    assert eng.db.execute("SELECT status FROM incidents WHERE id=1").fetchone()[0] is None


# ---------- журнал ----------
def test_audit_read_is_itself_audited(demo_client):
    login_as(demo_client, "admin")
    demo_client.put("/api/layout", json=LAYOUT)
    rows = demo_client.get("/api/audit?limit=1").json()
    assert len(rows) == 1 and rows[0]["action"] == "layout_save" and rows[0]["result"] == "ok"
    assert {"id", "ts", "user", "role", "action", "target", "result", "ip", "prev_hash", "hash"} <= set(rows[0])
    row = _audit(demo_client).read()[0]
    assert (row["action"], row["result"], row["user"]) == ("audit_read", "ok", "alice")


def test_audit_read_limit_is_clamped(demo_client):
    login_as(demo_client, "admin")
    for _ in range(3):
        demo_client.delete("/api/layout")   # по 2 рядки: started і ok
    assert len(demo_client.get("/api/audit?limit=-5").json()) == 1
    assert len(demo_client.get("/api/audit").json()) == 7   # 6 layout_reset + попереднє audit_read


def test_audit_read_limit_has_a_ceiling_of_1000(demo_client):
    from dronewatch.service.audit import AuditEntry
    audit, eng = _audit(demo_client), demo_client.app.state.runtime.engine
    with eng.lock:   # 1100 рядків одним комітом: швидше, ніж 1100 разів append
        for i in range(1100):
            audit.append_locked(AuditEntry("bulk", "admin", "layout_save", f"n={i}", "ok", "1.2.3.4"))
        eng.db.commit()
    login_as(demo_client, "admin")
    rows = demo_client.get("/api/audit?limit=5000").json()
    assert len(rows) == 1000 and rows[0]["target"] == "n=1099", "найновіші першими, не більше 1000"
    assert _audit(demo_client).read()[0]["target"] == "limit=1000"


def test_audit_read_blocked_when_audit_fails(demo_client, monkeypatch):
    login_as(demo_client, "admin")
    monkeypatch.setattr(_audit(demo_client), "append", Mock(side_effect=AuditError("ro")))
    assert demo_client.get("/api/audit").status_code == 503


# ---------- WebSocket ----------
def test_ws_rejects_unknown_role(demo_client):
    demo_client.cookies.set(COOKIE, demo_client.app.state.access.sessions.create("alice", "root"))  # type: ignore[arg-type]
    with pytest.raises(WebSocketDisconnect) as exc, demo_client.websocket_connect("/ws"):
        pass
    assert exc.value.code == 1008
    assert not demo_client.app.state.runtime.clients


def test_ws_viewer_gets_state_without_audit(demo_client):
    login_as(demo_client, "viewer")
    with demo_client.websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert len(demo_client.app.state.runtime.clients) == 1

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code=1000):
            pass
    rt, fake = demo_client.app.state.runtime, FakeWs()
    rt.clients = {fake: demo_client.cookies.get(COOKIE)}
    asyncio.run(app.scan_step(app.app, rt))
    assert fake.sent and not any("audit" in k for k in fake.sent[0]), "viewer по WebSocket не бачить журналу"


def test_me_returns_role(demo_client):
    login_as(demo_client, "operator", user="olga")
    assert demo_client.get("/api/me").json() == {"user": "olga", "role": "operator", "auth": True}


# ---------- доступ за конфігом ----------
def test_demo_with_users_requires_login():
    cfg = config.Config.model_validate({"mode": "demo", "dashboard": {"users": [{"name": "alice", "password_hash": FAST, "role": "admin"}]}})
    access = app.build_dashboard_access(cfg)
    assert access.enabled and access.users.authenticate("alice", PASSWORD) == "admin"


def test_demo_without_users_is_view_only(caplog, tmp_path, monkeypatch):
    with caplog.at_level("WARNING", logger="dronewatch"):
        access = app.build_dashboard_access(config.Config(mode="demo"))
    assert not access.enabled and caplog.records
    _demo_runtime(tmp_path, monkeypatch)
    app.app.state.access = access
    c = TestClient(app.app, raise_server_exceptions=False)
    assert c.get("/api/me").json()["role"] == "viewer"
    assert c.put("/api/layout", json=LAYOUT).status_code == 403
