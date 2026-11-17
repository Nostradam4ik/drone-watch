"""Автентифікація дашборда: паролі, сесії, ліміт спроб, cookie, CSRF/Origin, WebSocket. Запуск: python test_dashboard.py (або pytest)."""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
import dashboard_auth as da
from dashboard_auth import LoginLimiter, SessionStore, Users

PASSWORD = "Correct-Horse-Battery-42"
# scrypt із меншим N лише для швидкості тестів: формат і перевірка ті самі, що з продакшн-параметрами
FAST = da.hash_password(PASSWORD)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------- паролі ----------
def test_password_hash_roundtrip_and_format():
    assert FAST.startswith("scrypt$16384$8$1$") and FAST.count("$") == 5
    assert da.verify_password(PASSWORD, FAST)
    assert not da.verify_password(PASSWORD + "x", FAST) and not da.verify_password("", FAST)
    assert da.verify_password("пароль-з-юнікодом-ЇєІ", da.hash_password("пароль-з-юнікодом-ЇєІ"))
    assert da.hash_password(PASSWORD) != da.hash_password(PASSWORD), "сіль має бути випадковою"
    assert PASSWORD not in FAST


def test_hash_parsing_rejects_garbage_and_resource_bombs():
    good = da.parse_hash(FAST)
    assert (good.n, good.r, good.p) == (16384, 8, 1) and len(good.salt) == 16 and len(good.digest) == 32
    for bad in ("", "plain", "scrypt$1$2", "bcrypt$16384$8$1$AAAAAAAAAAA=$AAAAAAAAAAAAAAAAAAAAAA==",
                "scrypt$16384$8$1$!!!$AAAA", "scrypt$abc$8$1$AAAAAAAAAAA=$AAAAAAAAAAAAAAAAAAAAAA==",
                "ЗАМІНІТЬ: python keygen.py --dashboard-user operator"):
        try:
            da.parse_hash(bad)
        except ValueError:
            continue
        raise AssertionError(f"мало відхилити: {bad!r}")
    salt, digest = "AAAAAAAAAAA=", "AAAAAAAAAAAAAAAAAAAAAA=="
    for n, r, p in ((2**30, 8, 1), (16385, 8, 1), (16384, 99, 1), (16384, 8, 99), (1, 8, 1)):   # гігабайти памʼяті, не степінь 2, завеликі r/p
        try:
            da.parse_hash(f"scrypt${n}${r}${p}${salt}${digest}")
        except ValueError:
            continue
        raise AssertionError(f"параметри {(n, r, p)} мали бути відхилені")


def _count_scrypt_calls(monkeypatch) -> list[int]:
    "Підміняє дороге хешування лічильником викликів (результат лишається справжнім)."
    calls: list[int] = []
    original = da._scrypt

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(da, "_scrypt", counting)
    return calls


def test_overlong_password_is_rejected_before_hashing(monkeypatch):
    calls = _count_scrypt_calls(monkeypatch)
    assert da.verify_password("x" * (da.MAX_PASSWORD_LEN + 1), FAST) is False
    assert calls == [], "довгий пароль не має доходити до дорогого хешування"


def test_unknown_user_costs_the_same_as_known_one(monkeypatch):
    """Інакше за часом відповіді видно, які імена існують: хешуємо і для неіснуючого імені."""
    users = Users({"alice": FAST})
    calls = _count_scrypt_calls(monkeypatch)
    assert users.authenticate("alice", PASSWORD) is True
    known = len(calls)
    assert users.authenticate("alice", "wrong-password-123") is False
    assert users.authenticate("nobody", PASSWORD) is False, "навіть правильний пароль іншого користувача не годиться"
    assert len(calls) == known * 3, "кожна спроба, відома чи ні, має робити однакову дорогу перевірку"


# ---------- сесії й ліміт ----------
def test_session_lifecycle():
    clock = Clock()
    store = SessionStore(60, clock=clock)
    token = store.create("alice")
    assert store.get(token).user == "alice"
    assert all(token not in key for key in store._items), "сирий токен не має лежати на сервері, лише його хеш"
    clock.now += 59
    assert store.get(token) is not None
    clock.now += 2
    assert store.get(token) is None, "сесія має скінчитися через ttl"
    token2 = store.create("bob")
    assert store.revoke(token2) is True and store.get(token2) is None and store.revoke(token2) is False
    for junk in (None, "", "x" * 1000, "не-токен"):
        assert store.get(junk) is None and store.revoke(junk) is False
    assert store.create("a") != store.create("a"), "токени унікальні"


def test_session_store_is_bounded():
    clock = Clock()
    store = SessionStore(60, clock=clock, max_sessions=5)
    tokens = []
    for i in range(12):
        clock.now += 1
        tokens.append(store.create(f"u{i}"))
    assert len(store._items) <= 5
    assert store.get(tokens[-1]) is not None and store.get(tokens[0]) is None, "витісняється найстаріша"


def test_login_limiter():
    clock = Clock()
    lim = LoginLimiter(max_failures=3, window_s=100, clock=clock)
    assert [lim.attempt("ip1") for _ in range(3)] == [0, 0, 0], "перші max_failures спроб дозволені"
    assert 99 <= lim.attempt("ip1") <= 100 and lim.attempt("ip2") == 0, "ліміт на адресу, а не глобальний"
    clock.now += 60
    assert 39 <= lim.attempt("ip1") <= 40
    clock.now += 41
    assert lim.attempt("ip1") == 0, "після вікна можна знову"
    lim.reset("ip1")
    assert [lim.attempt("ip1") for _ in range(3)] == [0, 0, 0], "успішний вхід скидає лічильник"


def test_login_limiter_memory_is_bounded():
    clock = Clock()
    lim = LoginLimiter(max_failures=3, window_s=10, clock=clock, max_keys=50)
    for i in range(500):
        clock.now += 1
        lim.attempt(f"ip{i}")
    assert len(lim._attempts) <= 60, len(lim._attempts)


def test_limiter_cannot_be_bypassed_by_a_parallel_burst():
    """Регрес із перевірки безпеки: «перевірити ліміт, потім записати невдачу» дозволяло залпу з 40 запитів пройти повністю."""
    import threading
    lim = LoginLimiter(max_failures=8, window_s=300)
    go, allowed, lock = threading.Barrier(40), [], threading.Lock()

    def worker():
        go.wait()
        if lim.attempt("1.2.3.4") == 0:
            with lock:
                allowed.append(1)
    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 8, f"дозволено {len(allowed)} замість 8"


# ---------- веб-шар ----------
def _client(base_url="http://testserver", *, auth=True, ttl=60, max_failures=8):
    try:
        import test_health
        from fastapi.testclient import TestClient
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    m = test_health._app_with_engine()
    m.DB_PATH = _temp_incidents_db()   # інакше /api/incidents створить порожній service/incidents.db
    clock = Clock()
    from dashboard_web import DashboardAccess
    m.app.state.access = (DashboardAccess(Users({"alice": FAST}), SessionStore(ttl, clock=clock),
                                          LoginLimiter(max_failures=max_failures, window_s=300, clock=clock))
                          if auth else DashboardAccess())
    return TestClient(m.app, base_url=base_url, follow_redirects=False), m, clock


LOGIN = {"username": "alice", "password": PASSWORD}
_DB_DIR = tempfile.TemporaryDirectory()


def _temp_incidents_db() -> str:
    import sqlite3
    from contextlib import closing
    path = str(Path(_DB_DIR.name, "incidents.db"))
    with closing(sqlite3.connect(path)) as db:   # `with connect()` лише комітить і лишає файл відкритим (на Windows його тоді не видалити)
        db.execute("CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, ts REAL)")
        db.commit()
    return path


def _login(client, **overrides):
    return client.post("/api/login", json={**LOGIN, **overrides})


def test_everything_is_closed_without_a_session():
    made = _client()
    c, m, _ = made
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200 and "Увійти" in c.get("/login").text
    for path in ("/api/state", "/api/config", "/api/sensors", "/api/incidents", "/api/me"):
        r = c.get(path)
        assert r.status_code == 401, path
        assert "scrypt" not in r.text and "tracks" not in r.text
    health = c.get("/api/health")
    assert health.status_code == 200 and health.json() == {"ok": True}, "анонімний health: лише ok, без подробиць"
    from starlette.websockets import WebSocketDisconnect
    try:
        with c.websocket_connect("/ws"):
            raise AssertionError("WebSocket без входу не мав відкритися")
    except WebSocketDisconnect:
        pass
    assert not m.clients, "відхилений WebSocket не має потрапити в список розсилки"


def test_login_session_logout_flow():
    made = _client()
    c, m, _ = made
    wrong = _login(c, password="wrong-password-123")
    unknown = _login(c, username="nobody")
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json(), "не розкриваємо, чи існує ім'я"
    assert "set-cookie" not in wrong.headers

    ok = _login(c)
    assert ok.status_code == 200 and ok.json() == {"user": "alice"}
    cookie = ok.headers["set-cookie"].lower()
    assert "dw_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie, "по відкритому HTTP cookie без Secure (інакше браузер її не збереже); під HTTPS див. окремий тест"
    assert PASSWORD not in ok.text and PASSWORD not in cookie

    assert c.get("/api/state").status_code == 200
    assert c.get("/api/me").json() == {"user": "alice", "auth": True}
    assert c.get("/").status_code == 200 and "Drone Watch" in c.get("/").text
    assert c.get("/login").status_code == 303, "уже увійшли: форма входу не потрібна"
    full = c.get("/api/health").json()
    assert "scan" in full and "lost_sensors" in full, "після входу health повний"

    token = c.cookies.get("dw_session")
    with c.websocket_connect("/ws", headers={"cookie": f"dw_session={token}"}) as ws:
        ws.send_text("hi")
        assert len(m.clients) == 1 and list(m.clients.values()) == [token]

    out = c.post("/api/logout", json={})
    assert out.status_code == 200
    assert c.get("/api/state").status_code == 401, "після виходу сесія мертва"
    assert c.cookies.get("dw_session") is None


def test_cookie_is_secure_over_https():
    made = _client("https://testserver")
    c, _, _ = made
    assert "secure" in _login(c).headers["set-cookie"].lower()


def test_session_expires():
    made = _client(ttl=60)
    c, _, clock = made
    assert _login(c).status_code == 200 and c.get("/api/state").status_code == 200
    clock.now += 61
    assert c.get("/api/state").status_code == 401
    assert c.get("/").status_code == 303


def test_expired_session_closes_its_websocket():
    _, m, clock = _client(ttl=60)

    class FakeWs:
        def __init__(self):
            self.sent, self.closed = [], None

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code=1000):
            self.closed = code

    token = m.app.state.access.sessions.create("alice")
    live, ghost = FakeWs(), FakeWs()
    m.clients.update({live: token, ghost: "виданий-колись-токен"})
    asyncio.run(m.scan_step())
    assert live.sent and live.closed is None, "чинна сесія отримує стан"
    assert not ghost.sent and ghost.closed == 1008 and ghost not in m.clients, "недійсна сесія не отримує нічого й закривається"
    clock.now += 61
    asyncio.run(m.scan_step())
    assert live.closed == 1008 and not m.clients, "сесія скінчилась: WebSocket закрито"


def test_csrf_and_origin_protection():
    made = _client()
    c, m, _ = made
    form = c.post("/api/login", data=LOGIN)   # так відправляє форма з чужого сайту
    assert form.status_code == 415 and not c.cookies
    assert c.post("/api/login", json=LOGIN, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/login", content=json.dumps(LOGIN), headers={"content-type": "text/plain", "origin": "http://testserver"}).status_code == 415
    assert c.post("/api/login", json=LOGIN, headers={"origin": "http://testserver"}).status_code == 200, "свій Origin проходить"
    token = c.cookies.get("dw_session")
    assert c.post("/api/logout", json={}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/state").status_code == 200, "чужий сайт не міг вилогінити"
    from starlette.websockets import WebSocketDisconnect
    try:   # чинна cookie, але підключення з чужого сайту
        with c.websocket_connect("/ws", headers={"cookie": f"dw_session={token}", "origin": "https://evil.example"}):
            raise AssertionError("WebSocket з чужим Origin не мав відкритися")
    except WebSocketDisconnect:
        pass
    assert not m.clients
    assert _login(c, extra="поле").status_code == 422, "зайві поля в тілі входу відхиляються"


def test_brute_force_is_limited():
    made = _client(max_failures=5)
    c, _, clock = made
    for _ in range(5):
        assert _login(c, password="wrong-password-123").status_code == 401
    blocked = _login(c, password="wrong-password-123")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    assert _login(c).status_code == 429, "навіть правильний пароль не годиться, поки діє блокування"
    clock.now += 301
    assert _login(c).status_code == 200


def test_parallel_login_burst_cannot_exceed_the_limit():
    """Через HTTP, із повільною перевіркою пароля (як scrypt): скільки запитів дійшло до перевірки пароля."""
    made = _client(max_failures=8)
    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    c, m, _ = made
    users, checked, lock = m.app.state.access.users, [], threading.Lock()
    original = users.authenticate

    def slow(name, password):
        with lock:
            checked.append(1)
        _time.sleep(0.05)
        return original(name, password)
    users.authenticate = slow
    barrier = threading.Barrier(30)

    def hit(i):
        barrier.wait()
        return c.post("/api/login", json={"username": "alice", "password": f"wrong-guess-{i}-xxxxx"}).status_code
    with ThreadPoolExecutor(30) as pool:
        codes = list(pool.map(hit, range(30)))
    assert len(checked) <= 8, f"до перевірки пароля дійшло {len(checked)} запитів, ліміт 8"
    assert codes.count(401) <= 8 and codes.count(429) >= 22, (codes.count(401), codes.count(429))


def test_responses_carry_security_headers():
    made = _client()
    c, _, _ = made
    for r in (c.get("/login"), c.get("/api/state"), c.get("/api/health"), _login(c)):
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["referrer-policy"] == "no-referrer"


def test_logs_never_contain_passwords_and_are_injection_safe():
    made = _client()
    c, _, _ = made
    records, handler = [], logging.Handler()
    handler.emit = records.append
    root = logging.getLogger("dronewatch")
    root.addHandler(handler)
    old = root.level
    root.setLevel(logging.INFO)
    try:
        _login(c, password="Wrong-Secret-Password-1")
        _login(c, username="evil\nINFO fake line injected")
        _login(c)
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    text = "\n".join(r.getMessage() for r in records)
    assert "Wrong-Secret-Password-1" not in text and PASSWORD not in text
    assert "fake line injected" in text and "\nINFO fake" not in text, "переніс рядка в імені не має підробляти запис у лозі"
    assert any("вхід: alice" in r.getMessage() for r in records) and any("невдалий вхід" in r.getMessage() for r in records)


def test_auth_disabled_keeps_everything_open_as_before():
    made = _client(auth=False)
    c, _, _ = made
    for path in ("/", "/api/state", "/api/config", "/api/sensors", "/api/incidents"):
        assert c.get(path).status_code == 200, path
    assert c.get("/login").status_code == 303, "форми входу немає, коли входу не вимагають"
    assert c.post("/api/login", json=LOGIN).status_code == 404
    assert c.get("/api/me").json() == {"user": "(без входу)", "auth": False}
    assert "scan" in c.get("/api/health").json()


def test_a_dashboard_session_does_not_grant_sensor_access():
    """Дашборд і датчики мають різні облікові дані: оператор не може слати події, датчик не може читати стан."""
    made = _client()
    c, m, _ = made
    from test_auth import KEYS, det, make_auth
    m.engine.auth = make_auth()
    assert _login(c).status_code == 200
    body = [{k: v for k, v in det("rf1").items() if k != "key"}]
    assert c.post("/api/events", json=body).status_code == 401, "сесія оператора не замінює ключ датчика"
    anonymous = c.__class__(m.app, base_url="http://testserver", follow_redirects=False)
    assert anonymous.post("/api/events", json=body, headers={"X-API-Key": KEYS["rf1"]}).status_code == 200
    assert anonymous.get("/api/state").status_code == 401, "ключ датчика не відкриває дашборд"


# ---------- конфіг ----------
LIVE = """
mode: live
auth: {{mode: disabled}}
{dashboard}
zones: [{{name: z, radius_m: 100, level: WARNING}}]
sensors:
  - {{id: radar1, type: radar, position: [0, 0], max_range: 1500}}
"""


def _load(text):
    import config
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "c.yaml")
        path.write_text(text, encoding="utf-8")
        return config.load(path)


def test_dashboard_config_is_fail_closed():
    try:
        import config  # noqa: F401
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")

    def rejects(dashboard, fragment):
        with pytest.raises(SystemExit, match=re.escape(fragment)):
            _load(LIVE.format(dashboard=dashboard))

    rejects("", "немає жодного користувача дашборда")                                    # за замовчуванням required
    rejects("dashboard: {auth: required}", "немає жодного користувача дашборда")
    rejects('dashboard: {users: [{name: a, password_hash: "scrypt$1$2"}]}', "scrypt")
    rejects('dashboard: {users: [{name: a, password_hash: "ЗАМІНІТЬ: python keygen.py"}]}', "keygen.py --dashboard-user")
    rejects(f'dashboard: {{users: [{{name: "a b", password_hash: "{FAST}"}}]}}', "pattern")
    rejects(f'dashboard: {{users: [{{name: a, password_hash: "{FAST}"}}, {{name: a, password_hash: "{FAST}"}}]}}', "унікальними")
    rejects("dashboard: {auth: maybe}", "auth")
    rejects("dashboard: {auth: disabled, session_ttl_s: -5}", "session_ttl_s")

    cfg = _load(LIVE.format(dashboard=f'dashboard: {{users: [{{name: alice, password_hash: "{FAST}"}}], session_ttl_s: 600}}'))
    assert cfg.dashboard.users[0].name == "alice" and cfg.dashboard.session_ttl_s == 600
    assert _load(LIVE.format(dashboard="dashboard: {auth: disabled}")).dashboard.auth == "disabled"
    assert _load("mode: demo\n").mode == "demo", "демо без входу стартує як і раніше"


def test_build_access_follows_mode_and_config():
    try:
        import app
        import config  # noqa: F401
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    assert not app.build_dashboard_access(_load("mode: demo\n")).enabled
    assert not app.build_dashboard_access(_load(LIVE.format(dashboard="dashboard: {auth: disabled}"))).enabled
    on = app.build_dashboard_access(_load(LIVE.format(dashboard=f'dashboard: {{users: [{{name: alice, password_hash: "{FAST}"}}]}}')))
    assert on.enabled and on.users.authenticate("alice", PASSWORD) and on.sessions.ttl_s == 8 * 3600


def test_demo_stack_has_a_working_dashboard_user():
    """Пароль демо-користувача в compose відповідає хешу в deploy/config.yaml, а емулятор входить цим користувачем."""
    try:
        import yaml
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    password = compose["emulator"]["environment"]["DRONEWATCH_DASHBOARD_PASSWORD"]
    users = yaml.safe_load((ROOT / "deploy" / "config.yaml").read_text(encoding="utf-8"))["dashboard"]["users"]
    assert [u["name"] for u in users] == ["demo"] and da.verify_password(password, users[0]["password_hash"])
    assert "--dashboard-user" in compose["emulator"]["command"] and "demo" in compose["emulator"]["command"]
    assert all("DASHBOARD_PASSWORD" not in str(c) for c in compose["emulator"]["command"]), "пароль не в аргументах (їх видно в списку процесів)"
    assert "DASHBOARD" not in " ".join(str(v) for v in compose["service"]["environment"]), "пароль оператора не потрібен самому сервісу"


# ---------- keygen ----------
def _keygen(*args, env=None):
    return subprocess.run([sys.executable, str(HERE / "keygen.py"), *args], capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, **(env or {})})


def test_keygen_dashboard_user():
    out = _keygen("--dashboard-user", "operator", "--generate")
    assert out.returncode == 0
    password = next(line.rsplit(": ", 1)[1] for line in out.stdout.splitlines() if "сервер його не зберігає" in line)
    encoded = next(line.split('password_hash: "')[1].rstrip('"}') for line in out.stdout.splitlines() if "password_hash" in line)
    assert len(password) >= da.MIN_PASSWORD_LEN and da.verify_password(password, encoded) and password not in encoded

    via_env = _keygen("--dashboard-user", "ops", "--password-env", "DASH_PW", env={"DASH_PW": "a-sufficiently-long-pass"})
    assert via_env.returncode == 0 and "a-sufficiently-long-pass" not in via_env.stdout, "пароль зі змінної не друкується"

    short = _keygen("--dashboard-user", "ops", "--password-env", "DASH_PW", env={"DASH_PW": "short-pw"})
    assert short.returncode == 2 and "short-pw" not in short.stdout + short.stderr and "12" in short.stderr
    assert _keygen("--dashboard-user", "a b", "--generate").returncode == 2
    assert _keygen("--dashboard-user", "ops", "--password-env", "NO_SUCH_VAR_123").returncode == 2


# ---------- справжній HTTPS + емулятор ----------
def test_real_https_login_cookie_and_emulator():
    for module in ("cryptography", "uvicorn", "app"):
        pytest.importorskip(module)
    try:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    import urllib.error
    import urllib.request

    from test_https import HttpsServer
    from test_tls import make_pki

    import emulate
    from dashboard_web import DashboardAccess

    with tempfile.TemporaryDirectory() as d:
        ca, crt, key = make_pki(d, names=("localhost", "127.0.0.1", "service"))
        srv = HttpsServer(crt, key)
        srv.app.app.state.access = DashboardAccess(Users({"alice": FAST}), SessionStore(300), LoginLimiter())
        try:
            base = f"https://127.0.0.1:{srv.port}"   # 127.0.0.1 (IP-SAN), не localhost: на Windows ::1 дає зайві ~2 с
            ctx = emulate.tls_context(str(ca))
            emulate.HTTPS_CONTEXT = ctx

            with pytest.raises(urllib.error.HTTPError) as exc:
                emulate.http(base + "/api/config")
            assert exc.value.code == 401, "без входу /api/config мав дати 401"

            with pytest.raises(urllib.error.HTTPError) as exc:
                emulate.http(base + "/api/login", {"username": "alice", "password": "wrong-password-123"})
            assert exc.value.code == 401, "хибний пароль мав дати 401"

            emulate.http(base + "/api/login", {"username": "alice", "password": PASSWORD})
            assert "sensors" in emulate.http(base + "/api/config"), "емулятор після входу читає конфіг"
            processor = next(h for h in emulate.opener().handlers if isinstance(h, urllib.request.HTTPCookieProcessor))
            cookies = [c for c in processor.cookiejar if c.name == "dw_session"]
            assert cookies and cookies[0].secure, "під HTTPS cookie має бути Secure"

            with connect(f"wss://127.0.0.1:{srv.port}/ws", ssl=ctx, open_timeout=5,
                         additional_headers={"Cookie": f"dw_session={cookies[0].value}"}) as ws:
                ws.send("hi")
            with pytest.raises(InvalidStatus) as exc, connect(f"wss://127.0.0.1:{srv.port}/ws", ssl=ctx, open_timeout=5):
                pass
            assert exc.value.response.status_code == 403, "wss без cookie має відхилятися"
        finally:
            emulate.HTTPS_CONTEXT = None
            srv.stop()


