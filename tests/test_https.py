"""HTTPS для HTTP API, дашборда і WebSocket. Запуск: python test_https.py (або pytest).

Інтеграційні тести піднімають справжній сервіс (uvicorn) із реальним сертифікатом і ганяють проти нього справжні клієнти:
емулятор, healthcheck.py і WebSocket. Потрібні cryptography, fastapi і uvicorn; без них відповідні тести пропускаються."""
import os
import re
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
        import uvicorn

        import test_health
        from dronewatch.service import app
        self.rt = test_health._runtime()   # стан сервісу (рушій, клієнти, задача скану)
        self.api = app.app                 # FastAPI-застосунок
        self.rt.engine.auth = make_auth()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(self.api, host="127.0.0.1", port=self.port, ssl_certfile=str(certfile),
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
    for module in ("cryptography", "uvicorn", "dronewatch.service.app"):   # сервіс потребує fastapi, paho, yaml: ті самі залежності, що й сервіс
        pytest.importorskip(module)
    return True


def ws_session(url, ctx, headers=None, hold=None):
    """Відкриває WebSocket по TLS, шле «hi» і, поки з'єднання відкрите, викликає hold(). Відмова сервера дає InvalidStatus.

    Навмисно asyncio-клієнт, а не websockets.sync: синхронний клієнт читає й пише в один SSL-сокет із двох потоків, і поверх
    TLS рукостискання до ~30 % спроб «зависає» до таймауту, хоча сервер відповів (сирий сокет і asyncio-клієнт: 0 збоїв із 30).
    """
    import asyncio

    from websockets.asyncio.client import connect

    async def run():
        async with connect(url, ssl=ctx, open_timeout=5, additional_headers=headers) as ws:
            await ws.send("hi")
            if hold is not None:
                await asyncio.to_thread(hold)

    asyncio.run(run())


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
        from dronewatch.service import serve
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    with tempfile.TemporaryDirectory() as d:
        crt, key = Path(d, "c.crt"), Path(d, "c.key")
        crt.write_text("x")
        key.write_text("x")

        def refuses(env, fragment):
            with pytest.raises(SystemExit, match=re.escape(fragment)):
                serve.settings(env)

        refuses({}, "HTTPS не налаштовано")                                                    # за замовчуванням: без TLS не стартуємо
        refuses({"ALLOW_INSECURE_HTTP": "0"}, "HTTPS не налаштовано")                          # лише рівно «1» вимикає захист
        refuses({"ALLOW_INSECURE_HTTP": "true"}, "HTTPS не налаштовано")
        refuses({"TLS_CERT_FILE": str(crt)}, "разом")
        refuses({"TLS_KEY_FILE": str(key)}, "разом")
        refuses({"TLS_CERT_FILE": str(crt), "TLS_KEY_FILE": str(Path(d, "немає.key"))}, "TLS_KEY_FILE")
        refuses({"TLS_CERT_FILE": str(Path(d, "немає.crt")), "TLS_KEY_FILE": str(key)}, "TLS_CERT_FILE")

        configured = serve.settings({"TLS_CERT_FILE": str(crt), "TLS_KEY_FILE": str(key), "PORT": "9443", "HOST": "127.0.0.1"})
        assert configured == serve.ServerSettings("127.0.0.1", 9443, str(crt), str(key))
        # TLS задано разом із прапорцем відкритого режиму: перемагає TLS
        assert serve.settings({"TLS_CERT_FILE": str(crt), "TLS_KEY_FILE": str(key), "ALLOW_INSECURE_HTTP": "1"}).ssl_certfile == str(crt)


def test_launcher_insecure_mode_is_explicit_and_loud():
    try:
        from dronewatch.service import serve
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    import io
    from contextlib import redirect_stderr
    err = io.StringIO()
    with redirect_stderr(err):
        insecure = serve.settings({"ALLOW_INSECURE_HTTP": "1"})
        assert insecure.ssl_certfile is None and insecure.ssl_keyfile is None
    assert "УВАГА" in err.getvalue() and "відкритим текстом" in err.getvalue()


# ---------- справжній HTTPS-сервер ----------
def test_https_api_dashboard_and_clients():
    from dronewatch.emulator import emulate
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
            before = dict(srv.rt.engine.stats)

            # 2. сертифікат від недовіреного CA: відхиляється ще до відправки запиту з ключем
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(other_ca))
            with pytest.raises(urllib.error.URLError) as exc:
                emulate.http(base + "/api/events", [ev], key=KEYS["rf1"])
            assert isinstance(exc.value.reason, ssl.SSLCertVerificationError), "недовірений CA мав бути відхилений"

            # 3. відкритий HTTP на TLS-порт: запит із ключем не доходить до застосунку
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(ca))
            try:
                emulate.http(f"http://localhost:{srv.port}/api/events", [ev], key=KEYS["rf1"])
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            else:
                raise AssertionError("відкритий HTTP на TLS-порту має не працювати")
            time.sleep(0.3)
            assert dict(srv.rt.engine.stats) == before, "відкритий запит із ключем не мав дійти до застосунку"

            # 4. дашборд і WebSocket по TLS
            html = urllib.request.urlopen(base + "/", context=emulate.HTTPS_CONTEXT, timeout=5).read().decode("utf-8")
            js = urllib.request.urlopen(base + "/static/app/main.js", context=emulate.HTTPS_CONTEXT, timeout=5).read().decode("utf-8")
            assert "Drone Watch" in html and 'location.protocol === "https:" ? "wss://"' in js, "фронтенд має сам обирати wss під https"
            pytest.importorskip("websockets.asyncio.client")

            def hold():
                assert _wait(lambda: len(srv.rt.clients) == 1), "WebSocket по TLS не зареєструвався"
                assert srv.rt.clients, "клієнт має бути в списку розсилки"
            ws_session(f"wss://127.0.0.1:{srv.port}/ws", emulate.HTTPS_CONTEXT, hold=hold)
        finally:
            emulate.HTTPS_CONTEXT = None
            srv.stop()

        # 5. ім'я хоста поза SAN: сертифікат від довіреного CA, але видано на інше ім'я
        srv2 = HttpsServer(wrong_crt, wrong_key)
        try:
            emulate.HTTPS_CONTEXT = emulate.tls_context(str(wrong_ca))
            with pytest.raises(urllib.error.URLError) as exc:
                emulate.http(f"https://localhost:{srv2.port}/api/health")
            reason = exc.value.reason
            assert isinstance(reason, ssl.SSLCertVerificationError) and "mismatch" in str(reason).lower(), "ім'я поза SAN мало бути відхилене"
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
                return subprocess.run([sys.executable, "-m", "dronewatch.service.healthcheck"], env=e, capture_output=True, text=True, timeout=15)
            ok = run_check(TLS_CERT_FILE="x", TLS_CA_FILE=str(ca))
            assert ok.returncode == 0, ok.stderr
            bad = run_check(TLS_CERT_FILE="x", TLS_CA_FILE=str(other_ca))
            assert (bad.returncode == 1 and "SSLCertVerificationError" in bad.stderr) or "URLError" in bad.stderr, bad.stderr
            plain = run_check()   # без TLS_CERT_FILE іде по відкритому HTTP: на TLS-порту має не вийти
            assert plain.returncode == 1, "healthcheck по відкритому HTTP не мав «пройти» на TLS-порту"
            srv.rt.scan_task = type("T", (), {"done": lambda self: True})()   # сервіс живий, але цикл сканів помер: /api/health дає 503
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

    def holders(vol):
        return {n for n, s in sv.items() if any(str(v).startswith(vol + ":") for v in s.get("volumes", []))}

    # sandbox це той самий сервіс у demo-режимі (профіль sandbox): так само лише HTTPS, лише localhost, без відкритого режиму
    assert holders("certs-http") == {"certs", "service", "sandbox", "ha-a", "ha-b"} and holders("certs-server") == {"certs", "mosquitto"}
    sb = sv["sandbox"]
    assert sb["ports"] == ["127.0.0.1:8001:8000"] and "ALLOW_INSECURE_HTTP" not in sb["environment"]
    assert sb["environment"]["TLS_CERT_FILE"] == "/tls/http.crt" and sb["profiles"] == ["sandbox"]
    assert all(str(v).endswith(":ro") for v in sb["volumes"] if str(v).startswith(("certs-http:", "certs-ca:")))
    assert "certs-http" in compose["volumes"]
    assert all(str(v).endswith(":ro") for n in ("service", "ha-a", "ha-b") for v in sv[n]["volumes"] if str(v).startswith(("certs-http:", "certs-ca:")))
    assert all(str(v).endswith(":ro") for v in sv["emulator"]["volumes"] if str(v).startswith("certs-ca:"))

    cmd = sv["emulator"]["command"]
    assert cmd[cmd.index("--url") + 1] == "https://service:8000" and "--ca" in cmd, "емулятор ходить на сервіс по HTTPS з перевіркою CA"

    dockerfile = (ROOT / "docker" / "service.Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "dronewatch.service.serve"]' in dockerfile, "точка входу має бути лаунчером, який не стартує без TLS"
    assert "uvicorn" not in dockerfile.split("CMD")[-1], "образ не має запускати uvicorn напряму, в обхід перевірки TLS"
    assert 'HEALTHCHECK' in dockerfile and "dronewatch.service.healthcheck" in dockerfile and "http://127.0.0.1" not in dockerfile

    script = (ROOT / "deploy" / "gen-certs.sh").read_text(encoding="utf-8")
    assert "HTTP_SANS" in script and "http.key" in script and "DNS:service" in script, "генератор має видавати сертифікат сервісу"


