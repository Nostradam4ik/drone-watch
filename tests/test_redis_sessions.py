"""Спільні сесії й ліміт входів у Redis: поведінка, fail-closed при збої, два «процеси» бачать одну сесію."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dronewatch.service import app, config, dashboard_redis
from dronewatch.service.dashboard_auth import SessionStoreError, check_redis_url
from dronewatch.service.dashboard_redis import RedisLoginLimiter, RedisSessionStore
from dronewatch.service.dashboard_web import COOKIE, DashboardAccess
from test_dashboard import FAST


class FakeRedis:
    """Мінімум Redis для цих класів; down=True імітує обрив (текст помилки містить «пароль»)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("redis://:secret@host down")

    def set(self, k, v, px=None, nx=False):
        self._check()
        if nx and k in self.data:
            return None
        self.data[k], self.ttl[k] = str(v), px
        return True

    def get(self, k):
        self._check()
        return self.data.get(k)

    def delete(self, k):
        self._check()
        self.ttl.pop(k, None)
        return 1 if self.data.pop(k, None) is not None else 0

    def incr(self, k):
        self._check()
        self.data[k] = str(int(self.data[k]) + 1)
        return int(self.data[k])

    def pttl(self, k):
        self._check()
        return self.ttl.get(k, -1)

    def pexpire(self, k, ms):
        self.ttl[k] = ms

    def pipeline(self, transaction=True):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def set(self, *a, **kw):
        self.ops.append(("set", a, kw))

    def incr(self, *a):
        self.ops.append(("incr", a, {}))

    def pttl(self, *a):
        self.ops.append(("pttl", a, {}))

    def execute(self):
        return [getattr(self.r, n)(*a, **kw) for n, a, kw in self.ops]


def test_session_roundtrip_ttl_and_revoke():
    r = FakeRedis()
    s = RedisSessionStore(r, 60)
    token = s.create("alice", "admin")
    assert all(token not in k for k in r.data), "у Redis лише хеш токена"
    got = s.get(token)
    assert (got.user, got.role) == ("alice", "admin")
    assert next(iter(r.ttl.values())) == 60_000
    assert s.get("інший") is None and s.get(None) is None and s.get("x" * 300) is None
    assert s.revoke(token) is True and s.get(token) is None and s.revoke(token) is False


def test_two_processes_share_sessions():
    r = FakeRedis()
    token = RedisSessionStore(r, 60).create("alice", "operator")
    assert RedisSessionStore(r, 60).get(token).role == "operator"


def test_corrupt_record_is_not_a_session():
    r = FakeRedis()
    s = RedisSessionStore(r, 60)
    r.data[s._key("tok")] = "не json"
    assert s.get("tok") is None


def test_redis_failure_is_an_error_not_no_session():
    r = FakeRedis()
    s = RedisSessionStore(r, 60)
    token = s.create("alice", "admin")
    r.down = True
    for call in (lambda: s.get(token), lambda: s.create("a", "viewer"), lambda: s.revoke(token)):
        with pytest.raises(SessionStoreError) as e:
            call()
        assert "secret" not in str(e.value), "текст помилки клієнта (там може бути пароль) не виходить назовні"


def test_limiter_blocks_after_max_and_is_shared_and_reset():
    r = FakeRedis()
    a, b = RedisLoginLimiter(r, max_failures=3, window_s=300), RedisLoginLimiter(r, max_failures=3, window_s=300)
    assert [a.attempt("ip"), b.attempt("ip"), a.attempt("ip")] == [0.0, 0.0, 0.0]
    assert 0 < b.attempt("ip") <= 300   # четверта спроба з іншого «процесу» вже заблокована
    assert a.attempt("other") == 0.0
    a.reset("ip")
    assert b.attempt("ip") == 0.0


def test_limiter_repairs_a_key_without_ttl():
    r = FakeRedis()
    r.data["dw:login:ip"] = "99"   # чужий запис без TTL не має блокувати назавжди
    lim = RedisLoginLimiter(r, max_failures=3, window_s=300)
    assert 0 < lim.attempt("ip") <= 300 and r.ttl["dw:login:ip"] == 300_000


def test_limiter_failure_is_an_error():
    r = FakeRedis()
    r.down = True
    with pytest.raises(SessionStoreError):
        RedisLoginLimiter(r).attempt("ip")
    with pytest.raises(SessionStoreError):
        RedisLoginLimiter(r).reset("ip")


@pytest.mark.parametrize("url", ["rediss://redis.internal:6380/0", "redis://localhost", "redis://127.0.0.1:6379", "redis://[::1]:6379"])
def test_redis_url_accepted(url):
    assert check_redis_url(url) == url


@pytest.mark.parametrize("url", ["redis://redis.internal", "http://localhost", "rediss://", "localhost:6379", "redis://10.0.0.5"])
def test_redis_url_rejected(url):
    with pytest.raises(ValueError, match="redis_url"):
        check_redis_url(url)


def test_config_rejects_plain_remote_redis():
    with pytest.raises(ValueError, match="redis_url"):
        config.DashboardCfg(session_store="redis", redis_url="redis://redis.internal")


def _redis_cfg() -> config.Config:
    return config.Config(mode="demo", dashboard=config.DashboardCfg(
        session_store="redis", users=[config.DashboardUser(name="a", role="admin", password_hash=FAST)]))


def test_build_access_needs_url_and_fails_closed_when_down(monkeypatch):
    monkeypatch.delenv("DRONEWATCH_REDIS_URL", raising=False)
    with pytest.raises(ValueError, match="redis_url"):
        app.build_dashboard_access(_redis_cfg())
    monkeypatch.setenv("DRONEWATCH_REDIS_URL", "redis://localhost:1")   # порт без слухача: сервіс не стартує
    with pytest.raises(SessionStoreError):
        app.build_dashboard_access(_redis_cfg())


def test_build_access_uses_redis_classes(monkeypatch):
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: FakeRedis())
    monkeypatch.setenv("DRONEWATCH_REDIS_URL", "rediss://redis.internal")
    access = app.build_dashboard_access(_redis_cfg())
    assert isinstance(access.sessions, RedisSessionStore) and isinstance(access.limiter, RedisLoginLimiter)


@pytest.fixture
def redis_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.app.state.runtime = app.build_runtime(config.Config(mode="demo"))
    fake = FakeRedis()
    app.app.state.access = DashboardAccess(sessions=RedisSessionStore(fake, 3600), limiter=RedisLoginLimiter(fake))
    c = TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)
    c.cookies.set(COOKIE, app.app.state.access.sessions.create("alice", "admin"))
    return c, fake


def test_http_session_works_then_503_when_redis_dies(redis_client):
    c, fake = redis_client
    assert c.get("/api/me").status_code == 200
    fake.down = True
    r = c.get("/api/me")
    assert r.status_code == 503 and r.json() == {"detail": "Сховище сесій недоступне"} and "secret" not in r.text


def test_ws_refused_when_redis_dies(redis_client):
    c, fake = redis_client
    fake.down = True
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws"):
        pass


def test_broadcast_sends_nothing_when_redis_dies(redis_client):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    _, fake = redis_client
    access = app.app.state.access
    token = access.sessions.create("alice", "viewer")
    ws = AsyncMock()
    rt = SimpleNamespace(clients={ws: token})
    asyncio.run(app.broadcast(rt, access, {"x": 1}))
    ws.send_json.assert_awaited_once()
    ws.send_json.reset_mock()
    fake.down = True
    asyncio.run(app.broadcast(rt, access, {"x": 1}))
    ws.send_json.assert_not_awaited()   # fail-closed: данні не йдуть, клієнт лишається в списку
    assert ws in rt.clients


def test_session_dies_when_user_is_removed_or_role_changed():
    from dronewatch.service.dashboard_auth import UserEntry, Users
    fake = FakeRedis()
    store = RedisSessionStore(fake, 3600)
    token = store.create("alice", "admin")
    same = DashboardAccess(Users({"alice": UserEntry(FAST, "admin")}), store, RedisLoginLimiter(fake))
    assert same.session_valid(token)
    for users in ({"bob": UserEntry(FAST, "admin")}, {"alice": UserEntry(FAST, "viewer")}):   # видалений / понижений
        access = DashboardAccess(Users(users), store, RedisLoginLimiter(fake))
        assert not access.session_valid(token)
