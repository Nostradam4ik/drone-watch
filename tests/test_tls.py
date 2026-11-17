"""TLS брокера: клієнти справді шифрують, перевіряють сертифікат і ім'я хоста. Запуск: python test_tls.py (або pytest).

Інтеграційні тести підіймають крихітний MQTT-брокер із РЕАЛЬНИМ TLS (сертифікати генерує cryptography) і ганяють проти нього
справжній код: MqttIngest (сервіс, paho) і MqttPublisher (емулятор). Без cryptography ці тести пропускаються."""
import datetime
import ipaddress
import json
import logging
import re
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
from dronewatch.fusion.sim import default_sensors
from dronewatch.service.engine import Engine, Zone

PASSWORD = "UNIQUE-BROKER-PASSWORD-7731"


# ---------- допоміжне: PKI і фейковий брокер ----------
def make_pki(directory, names=("localhost", "127.0.0.1"), cn="test CA"):
    """CA + серверний сертифікат із SAN=names. Повертає (ca.pem, server.pem, server.key)."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


    def name(common_name):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    now = datetime.datetime.now(datetime.UTC)
    window = {"not_valid_before": now - datetime.timedelta(days=1), "not_valid_after": now + datetime.timedelta(days=1)}

    def build(subject, issuer, pub, signer, extensions):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(pub)
             .serial_number(x509.random_serial_number()).not_valid_before(window["not_valid_before"]).not_valid_after(window["not_valid_after"]))
        for ext, critical in extensions:
            b = b.add_extension(ext, critical=critical)
        return b.sign(signer, hashes.SHA256())

    ca_key, srv_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    # SKI/AKI потрібні суворій перевірці X.509, яку Python 3.13+ вмикає за замовчуванням (VERIFY_X509_STRICT)
    ca = build(name(cn), name(cn), ca_key.public_key(), ca_key, [
        (x509.BasicConstraints(ca=True, path_length=0), True),
        (x509.KeyUsage(False, False, False, False, False, True, True, False, False), True),
        (x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)])
    sans = [x509.IPAddress(ipaddress.ip_address(n)) if n.replace(".", "").isdigit() else x509.DNSName(n) for n in names]
    srv = build(name("server"), ca.subject, srv_key.public_key(), ca_key, [
        (x509.BasicConstraints(ca=False, path_length=None), True),
        (x509.SubjectAlternativeName(sans), False),
        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
        (x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), False),
        (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)])
    d = Path(directory)
    paths = (d / f"{cn}-ca.pem", d / f"{cn}-server.pem", d / f"{cn}-server.key")
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(srv.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(srv_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return paths


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("закрито")
        buf += chunk
    return buf


def _packet(sock):
    first = _recv(sock, 1)[0]
    length, mult = 0, 1
    while True:
        d = _recv(sock, 1)[0]
        length += (d & 127) * mult
        if not d & 128:
            break
        mult *= 128
    return first, _recv(sock, length)


class FakeBroker:
    """Мінімальний MQTT 3.1.1 брокер поверх TLS: CONNACK, SUBACK, PINGRESP, запис PUBLISH; може штовхнути PUBLISH клієнту."""

    def __init__(self, certfile, keyfile, push=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile, keyfile)
        self.ctx, self.push = ctx, push
        self.connects, self.publishes, self.handshake_errors, self.plain_bytes = [], [], [], []
        self.versions = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, raw):
        raw.settimeout(5)
        try:
            tls = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as e:   # клієнт не вміє/не хоче TLS або відхилив наш сертифікат
            self.handshake_errors.append(type(e).__name__)
            raw.close()
            return
        self.versions.append(tls.version())
        try:
            while True:
                first, body = _packet(tls)
                kind = first >> 4
                if kind == 1:   # CONNECT: ім'я протоколу, рівень, прапори, keepalive, далі client id, [username], [password]
                    flags, i = body[7], 10
                    fields = []
                    cid_len = int.from_bytes(body[i:i + 2], "big")
                    cid = body[i + 2:i + 2 + cid_len].decode()
                    i += 2 + cid_len
                    for bit in (0x80, 0x40):
                        if flags & bit:
                            n = int.from_bytes(body[i:i + 2], "big")
                            fields.append(body[i + 2:i + 2 + n].decode())
                            i += 2 + n
                    self.connects.append({"client_id": cid, "username": fields[0] if fields else None,
                                          "password": fields[1] if len(fields) > 1 else None})
                    tls.sendall(b"\x20\x02\x00\x00")
                elif kind == 8:   # SUBSCRIBE -> SUBACK, і одразу штовхнути повідомлення, якщо просили
                    tls.sendall(b"\x90\x03" + body[:2] + b"\x00")
                    if self.push:
                        topic, payload = self.push
                        t = topic.encode()
                        tls.sendall(bytes([0x30, 2 + len(t) + len(payload)]) + len(t).to_bytes(2, "big") + t + payload)
                elif kind == 3:   # PUBLISH
                    n = int.from_bytes(body[:2], "big")
                    self.publishes.append((body[2:2 + n].decode(), json.loads(body[2 + n:])))
                elif kind == 12:  # PINGREQ
                    tls.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
        except (ConnectionError, OSError, ssl.SSLError):
            return

    def close(self):
        self.sock.close()


def _wait(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _optional_module(name: str):
    try:
        return __import__(name)
    except ImportError:
        return None


# ---------- тести ----------
def test_client_contexts_verify_by_default():
    """І в сервісі, і в емуляторі: перевірка сертифіката й імені хоста обов'язкова, не нижче TLS 1.2, довіра лише вказаному CA."""
    from dronewatch.emulator import emulate
    with tempfile.TemporaryDirectory() as d:
        ca, _, _ = make_pki(d)
        contexts = [("емулятор", emulate.tls_context(str(ca)))]
        if (mqtt_in := _optional_module("mqtt_in")) is not None:   # сервісна половина потребує paho; емуляторна ні
            contexts.append(("сервіс", mqtt_in.tls_context(str(ca))))
        for who, ctx in contexts:
            assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname, who
            assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2, who
            assert len(ctx.get_ca_certs()) == 1, f"{who}: має довіряти лише вказаному CA, а не всім системним"
    emulate.tls_context()   # без CA береться системне сховище; перевірка так само обов'язкова
    try:
        emulate.tls_context("/немає/такого/ca.pem")
    except OSError:
        pass
    else:
        raise AssertionError("неіснуючий CA має давати помилку, а не мовчки довіряти всьому")


def test_emulator_publisher_over_tls():
    from dronewatch.emulator import emulate
    with tempfile.TemporaryDirectory() as d:
        ca, crt, key = make_pki(d)
        broker = FakeBroker(crt, key)
        try:
            pub = emulate.MqttPublisher("127.0.0.1", broker.port, "rf1", PASSWORD, client_id="rf1", tls=emulate.tls_context(str(ca)))
            pub.publish("dronewatch/events/rf1", [{"sensor_id": "rf1", "key": "k", "heartbeat": True}])
            assert _wait(lambda: broker.publishes), "брокер не отримав повідомлення"
            pub.close()
            assert broker.connects[0] == {"client_id": "rf1", "username": "rf1", "password": PASSWORD}
            assert broker.publishes[0][0] == "dronewatch/events/rf1" and broker.publishes[0][1][0]["sensor_id"] == "rf1"
            assert broker.versions[0] in ("TLSv1.2", "TLSv1.3")
        finally:
            broker.close()


def test_plaintext_client_cannot_reach_a_tls_broker_and_leaks_nothing():
    """Відкритий клієнт проти TLS-порту: зʼєднання не встановлюється, а логін і пароль брокер не бачить."""
    from dronewatch.emulator import emulate
    with tempfile.TemporaryDirectory() as d:
        _, crt, key = make_pki(d)
        broker = FakeBroker(crt, key)
        try:
            try:
                emulate.MqttPublisher("127.0.0.1", broker.port, "rf1", PASSWORD, tls=None)
            except OSError:
                pass
            else:
                raise AssertionError("відкритий клієнт мав відхилитися TLS-брокером")
            assert _wait(lambda: broker.handshake_errors), "брокер мав зафіксувати невдалий TLS-хендшейк"
            assert broker.connects == [] and broker.publishes == [], "через відкритий канал нічого не пройшло"
        finally:
            broker.close()


def test_untrusted_and_mismatched_certificates_are_rejected():
    from dronewatch.emulator import emulate
    with tempfile.TemporaryDirectory() as d:
        ca, crt, key = make_pki(d)
        other_ca, _, _ = make_pki(d, cn="other CA")
        wrong_name_ca, wrong_crt, wrong_key = make_pki(d, names=("some.other.host",), cn="wrong name")
        good, wrong = FakeBroker(crt, key), FakeBroker(wrong_crt, wrong_key)
        try:
            def refused(port, ca_path, what):
                try:
                    emulate.MqttPublisher("127.0.0.1", port, "rf1", PASSWORD, tls=emulate.tls_context(str(ca_path)))
                except ssl.SSLCertVerificationError as e:
                    return str(e)
                raise AssertionError(f"мало бути відхилено: {what}")
            assert "verify failed" in refused(good.port, other_ca, "сертифікат від CA, якому клієнт не довіряє")
            assert "mismatch" in refused(wrong.port, wrong_name_ca, "ім'я хоста не в SAN").lower()
            assert good.connects == [] and wrong.connects == [], "до перевірки сертифіката облікові дані не передаються"
            # і нормальний випадок для контролю
            emulate.MqttPublisher("127.0.0.1", good.port, "rf1", PASSWORD, tls=emulate.tls_context(str(ca))).close()
            assert _wait(lambda: good.connects)
        finally:
            good.close()
            wrong.close()


def test_service_mqtt_over_tls_receives_events():
    """Справжній MqttIngest (paho) із TLS: підключення, підписка, прийом події, довіра лише своєму CA."""
    try:
        from dronewatch.service.mqtt_in import MqttIngest
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    with tempfile.TemporaryDirectory() as d:
        ca, crt, key = make_pki(d)
        other_ca, _, _ = make_pki(d, cn="other CA")
        event = {"sensor_id": "rf1", "ts": time.time(), "azimuth_deg": 12.0}
        broker = FakeBroker(crt, key, push=("dronewatch/events/rf1", json.dumps([event]).encode()))
        engine = Engine(default_sensors(), [Zone("z", 600, "WARNING")])
        ing = MqttIngest(engine, "127.0.0.1", broker.port, username="dronewatch-service", password=PASSWORD, ca_cert=str(ca))
        records, handler = [], logging.Handler()
        handler.emit = records.append
        logging.getLogger("dronewatch").addHandler(handler)
        bad = None
        try:
            assert ing.tls and ing.status()["tls"] is True
            ing.start()
            assert _wait(lambda: ing.status()["connected"]), "сервіс не підключився до TLS-брокера"
            assert _wait(lambda: engine.stats["events_in"] == 1), "подію через TLS не отримано"
            assert broker.connects[0]["username"] == "dronewatch-service" and broker.connects[0]["password"] == PASSWORD

            # той самий брокер, але клієнт довіряє іншому CA: не підключається, у лозі одна зрозуміла причина, без пароля
            bad = MqttIngest(engine, "127.0.0.1", broker.port, username="dronewatch-service", password=PASSWORD, ca_cert=str(other_ca))
            n = len(broker.connects)
            bad.start()
            time.sleep(3)
            assert bad.status()["connected"] is False and len(broker.connects) == n, "облікові дані пішли на недовірений брокер"
            texts = [r.getMessage() for r in records]
            assert sum("не вдалося зʼєднатися" in t for t in texts) == 1 and any("TLS" in t for t in texts), texts
            assert PASSWORD not in " ".join(texts)
        finally:
            logging.getLogger("dronewatch").removeHandler(handler)
            ing.stop()
            if bad:
                bad.stop()
            broker.close()

    with pytest.raises(RuntimeError, match=re.escape("/немає/такого/ca.pem")):   # хибний шлях до CA валить старт, називаючи шлях
        MqttIngest(engine, "127.0.0.1", 1, ca_cert="/немає/такого/ca.pem")


def test_demo_stack_is_tls_only_and_ships_no_private_keys():
    """Статична перевірка репозиторію й стенда."""
    try:
        import yaml
    except ImportError as e:
        pytest.skip(f"залежності відсутні: {e}")
    conf = [line.split("#")[0].strip() for line in (ROOT / "deploy" / "mosquitto.conf").read_text(encoding="utf-8").splitlines()]
    assert "listener 8883" in conf and not any(line.startswith("listener 1883") for line in conf), "відкритого слухача бути не повинно"
    for required in ("cafile /mosquitto/certs-ca/ca.crt", "certfile /mosquitto/certs-server/server.crt",
                     "keyfile /mosquitto/certs-server/server.key", "allow_anonymous false"):
        assert required in conf, required

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    sv = compose["services"]
    assert sv["mosquitto"]["ports"] == ["127.0.0.1:8883:8883"], "лише TLS-порт і лише на loopback"
    assert sv["mosquitto"]["depends_on"]["certs"]["condition"] == "service_completed_successfully"
    assert str(sv["service"]["environment"]["MQTT_PORT"]) == "8883" and sv["service"]["environment"]["MQTT_CA_CERT"] == "/certs/ca.crt"
    assert "--mqtt-ca" in sv["emulator"]["command"] and "mosquitto:8883" in sv["emulator"]["command"]
    assert "--cafile" in " ".join(sv["mosquitto"]["healthcheck"]["test"]) and "8883" in " ".join(sv["mosquitto"]["healthcheck"]["test"])
    # приватний ключ брокера бачить лише брокер (і генератор); сервіс та емулятор отримують тільки публічний CA
    users = {name for name, s in sv.items() if any(str(v).startswith("certs-server:") for v in s.get("volumes", []))}
    assert users == {"certs", "mosquitto"}, users
    assert all(str(v).endswith(":ro") for v in sv["service"]["volumes"] if str(v).startswith("certs-ca:"))
    assert not any(str(v).startswith("certs-server:") for v in sv["service"]["volumes"] + sv["emulator"]["volumes"])

    script = (ROOT / "deploy" / "gen-certs.sh").read_bytes()
    assert b"\r" not in script, "скрипт із CRLF не запуститься в контейнері"
    installs = [line for line in script.decode("utf-8").splitlines() if line.lstrip().startswith("install ")]
    assert installs and not any("ca.key" in line for line in installs), "ключ CA не має потрапляти в томи"

    marker = "PRIVATE " + "KEY-----"                          # зібрано з частин, щоб цей файл не знаходив сам себе
    offenders = []
    skipped_dirs = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        scannable = path.is_file() and not skipped_dirs & set(path.parts) and path.suffix not in (".png", ".pyc", ".db")
        if scannable and marker in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"у репозиторії є приватні ключі: {offenders}"


