"""HTTPS для HTTP API, дашборда і WebSocket. Запуск: python test_https.py (або pytest).

Інтеграційні тести піднімають справжній сервіс (uvicorn) із реальним сертифікатом і ганяють проти нього справжні клієнти:
емулятор, healthcheck.py і WebSocket. Потрібні cryptography, fastapi і uvicorn; без них відповідні тести пропускаються."""
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
from test_auth import KEYS, make_auth
from test_tls import make_pki


class HttpsServer:
    """Справжній сервіс (app.app) під uvicorn із TLS. lifespan вимкнено: рушій підставляє тест, як у TestClient."""

    def __init__(self, certfile, keyfile):
        import test_health
        import uvicorn
        self.app = test_health._app_with_engine()
        self.app.engine.auth = make_auth()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(self.app.app, host="127.0.0.1", port=self.port, ssl_certfile=str(certfile),
                                                    ssl_keyfile=str(keyfile), lifespan="off", log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        end = time.time() + 8
        while not self.server.started and time.time() < end:
            time.sleep(0.05)
        assert self.server.started, "сервер не стартував"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def _deps():
    try:
        import app  # noqa: F401  (потребує fastapi, paho, yaml: ті самі залежності, що й у сервісу)
        import cryptography  # noqa: F401
        import uvicorn
        return True
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------- лаунчер: fail-closed ----------
def test_launcher_refuses_to_start_without_tls():
    try:
        import serve
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    with tempfile.TemporaryDirectory() as d:
        crt, key = Path(d, "c.crt"), Path(d, "c.key")
        crt.write_text("x")
        key.write_text("x")

        def refuses(env, fragment):
            try:
                serve.settings(env)
            except SystemExit as e:
                assert fragment in str(e), (fragment, str(e))
                return
            raise AssertionError(f"мав відмовити: {env}")

        refuses({}, "HTTPS не налаштовано")                                                    # за замовчуванням: без TLS не стартуємо
        refuses({"ALLOW_INSECURE_HTTP": "0"}, "HTTPS не налаштовано")                          # лише рівно «1» вимикає захист
        refuses({"ALLOW_INSECURE_HTTP": "true"}, "HTTPS не налаштовано")
        refuses({"TLS_CERT_FILE": str(crt)}, "разом")
        refuses({"TLS_KEY_FILE": str(key)}, "разом")
        refuses({"TLS_CERT_FILE": str(crt), "TLS_KEY_FILE": str(Path(d, "немає.key"))}, "TLS_KEY_FILE")
        refuses({"TLS_CERT_FILE": str(Path(d, "немає.crt")), "TLS_KEY_FILE": str(key)}, "TLS_CERT_FILE")

        host, port, kw = serve.settings({"TLS_CERT_FILE": str(crt), "TLS_KEY_FILE": str(key), "PORT": "9443", "HOST": "127.0.0.1"})
        assert (host, port, kw) == ("127.0.0.1", 9443, {"ssl_certfile": str(crt), "ssl_keyfile": str(key)})
        # TLS задано разом із прапорцем відкритого режиму: перемагає TLS
        assert serve.settings({"TLS_CERT_FILE": str(crt), "TLS_KEY_FILE": str(key), "ALLOW_INSECURE_HTTP": "1"})[2]["ssl_certfile"]


def test_launcher_insecure_mode_is_explicit_and_loud():
    try:
        import serve
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    import io
    from contextlib import redirect_stderr
    err = io.StringIO()
    with redirect_stderr(err):
        assert serve.settings({"ALLOW_INSECURE_HTTP": "1"})[2] == {}
    assert "УВАГА" in err.getvalue() and "відкритим текстом" in err.getvalue()


# ---------- справжній HTTPS-сервер ----------
def test_https_api_dashboard_and_clients():
    import emulate
    with tempfile.TemporaryDirectory() as d:
        ca, crt, key = make_pki(d, names=("localhost", "127.0.0.1", "service"))
        other_ca, _, _ = make_pki(d, cn="other CA")
        wrong_ca, wrong_crt, wrong_key = make_pki(d, names=("some.other.host",), cn="wrong name")
        srv = HttpsServer(crt, key)
        try:
            # 127.0.0.1, а не localhost: на Windows клієнт спершу пробує IPv6 ::1 і ~2 с чекає, а подія за цей час
            # стає «застарілою» (STALE_S = 2). Для 127.0.0.1 у сертифіката є IP-SAN, перевірка так само сувора.
            base = f"https://127.0.0.1:{srv.port}"
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(ca))

            # 1. API по HTTPS з ключем датчика: усе працює
            health = emulate.http(base + "/api/health")
            assert health["auth"] is True and health["ok"] is True
            ev = {"sensor_id": "rf1", "ts": time.time(), "azimuth_deg": 10.0}
            assert emulate.http(base + "/api/events", [ev], key=KEYS["rf1"])["accepted"] == 1
            before = dict(srv.app.engine.stats)

            # 2. сертифікат від недовіреного CA: відхиляється ще до відправки запиту з ключем
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(other_ca))
            try:
                emulate.http(base + "/api/events", [ev], key=KEYS["rf1"])
            except urllib.error.URLError as e:
                assert isinstance(e.reason, ssl.SSLCertVerificationError), e.reason
            else:
                raise AssertionError("недовірений CA мав бути відхилений")

            # 3. відкритий HTTP на TLS-порт: запит із ключем не доходить до застосунку
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(ca))
            try:
                emulate.http(f"http://localhost:{srv.port}/api/events", [ev], key=KEYS["rf1"])
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            else:
                raise AssertionError("відкритий HTTP на TLS-порту має не працювати")
            time.sleep(0.3)
            assert dict(srv.app.engine.stats) == before, "відкритий запит із ключем не мав дійти до застосунку"

            # 4. дашборд і WebSocket по TLS
            html = urllib.request.urlopen(base + "/", context=emulate.HTTPS_CONTEXT, timeout=5).read().decode("utf-8")
            assert "Drone Watch" in html and 'location.protocol === "https:" ? "wss://"' in html, "фронтенд має сам обирати wss під https"
            connect = pytest.importorskip("websockets.sync.client").connect
            with connect(f"wss://127.0.0.1:{srv.port}/ws", ssl=emulate.HTTPS_CONTEXT, open_timeout=5) as ws:
                ws.send("hi")
                assert _wait(lambda: len(srv.app.clients) == 1), "WebSocket по TLS не зареєструвався"
                assert srv.app.clients, "клієнт має бути в списку розсилки"
        finally:
            emulate.HTTPS_CONTEXT = None
            srv.stop()

        # 5. ім'я хоста поза SAN: сертифікат від довіреного CA, але видано на інше ім'я
        srv2 = HttpsServer(wrong_crt, wrong_key)
        try:
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(wrong_ca))
            try:
                emulate.http(f"https://localhost:{srv2.port}/api/health")
            except urllib.error.URLError as e:
                assert isinstance(e.reason, ssl.SSLCertVerificationError) and "mismatch" in str(e.reason).lower(), e.reason
            else:
                raise AssertionError("ім'я поза SAN мало бути відхилене")
        finally:
            emulate.HTTPS_CONTEXT = None
            srv2.stop()


def test_healthcheck_script_verifies_the_certificate():
    with tempfile.TemporaryDirectory() as d:
        ca, crt, key = make_pki(d)
        other_ca, _, _ = make_pki(d, cn="other CA")
        srv = HttpsServer(crt, key)
        try:
            def run_check(**env):
                e = {**os.environ, "PORT": str(srv.port), **env}
                e.pop("TLS_CA_FILE", None) if "TLS_CA_FILE" not in env else None
                return subprocess.run([sys.executable, str(HERE / "healthcheck.py")], env=e, capture_output=True, text=True, timeout=15)
            ok = run_check(TLS_CERT_FILE="x", TLS_CA_FILE=str(ca))
            assert ok.returncode == 0, ok.stderr
            bad = run_check(TLS_CERT_FILE="x", TLS_CA_FILE=str(other_ca))
            assert bad.returncode == 1 and "SSLCertVerificationError" in bad.stderr or "URLError" in bad.stderr, bad.stderr
            plain = run_check()   # без TLS_CERT_FILE іде по відкритому HTTP: на TLS-порту має не вийти
            assert plain.returncode == 1, "healthcheck по відкритому HTTP не мав «пройти» на TLS-порту"
            srv.app.scan_task = type("T", (), {"done": lambda self: True})()   # сервіс живий, але цикл сканів помер: /api/health дає 503
            dead = run_check(TLS_CERT_FILE="x", TLS_CA_FILE=str(ca))
            assert dead.returncode == 1, "503 від /api/health має робити контейнер unhealthy"
        finally:
            srv.stop()


# ---------- статична перевірка стенда ----------
def test_demo_stack_serves_https_only():
    try:
        import yaml
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    sv = compose["services"]
    env = sv["service"]["environment"]
    assert env["TLS_CERT_FILE"] == "/tls/http.crt" and env["TLS_KEY_FILE"] == "/tls/http.key" and env["TLS_CA_FILE"] == "/certs/ca.crt"
    assert sv["service"]["ports"] == ["127.0.0.1:8000:8000"]
    assert "ALLOW_INSECURE_HTTP" not in env, "демо-стенд не має вмикати відкритий режим"
    assert sv["service"]["depends_on"]["certs"]["condition"] == "service_completed_successfully"

    # ключ сервісу бачить лише сервіс (і генератор), ключ брокера лише брокер, емулятор не бачить жодного ключа
    holders = lambda vol: {n for n, s in sv.items() if any(str(v).startswith(vol + ":") for v in s.get("volumes", []))}
    assert holders("certs-http") == {"certs", "service"} and holders("certs-server") == {"certs", "mosquitto"}
    assert "certs-http" in compose["volumes"]
    assert all(str(v).endswith(":ro") for n in ("service",) for v in sv[n]["volumes"] if str(v).startswith(("certs-http:", "certs-ca:")))
    assert all(str(v).endswith(":ro") for v in sv["emulator"]["volumes"] if str(v).startswith("certs-ca:"))

    cmd = sv["emulator"]["command"]
    assert cmd[cmd.index("--url") + 1] == "https://service:8000" and "--ca" in cmd, "емулятор ходить на сервіс по HTTPS з перевіркою CA"

    dockerfile = (HERE / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "serve.py"]' in dockerfile, "точка входу має бути лаунчером, який не стартує без TLS"
    assert "uvicorn" not in dockerfile.split("CMD")[-1], "образ не має запускати uvicorn напряму, в обхід перевірки TLS"
    assert 'HEALTHCHECK' in dockerfile and "healthcheck.py" in dockerfile and "http://127.0.0.1" not in dockerfile

    script = (ROOT / "deploy" / "gen-certs.sh").read_text(encoding="utf-8")
    assert "HTTP_SANS" in script and "http.key" in script and "DNS:service" in script, "генератор має видавати сертифікат сервісу"


