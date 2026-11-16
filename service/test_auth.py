"""Автентифікація датчиків ключем. Запуск: python test_auth.py (або pytest). Частина тестів потребує залежностей сервісу."""
import json
import logging
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fusion_proto"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "emulator"))
from auth import SensorAuth, hash_key  # noqa: E402
from engine import Engine, Zone  # noqa: E402
from sim import default_sensors  # noqa: E402

KEYS = {"radar1": "radar-key-" + "a" * 30, "rf1": "rf-key-" + "b" * 30, "ac1": "ac-key-" + "c" * 30}


def make_auth():
    return SensorAuth({sid: hash_key(k) for sid, k in KEYS.items()})


def make_engine():
    return Engine(default_sensors(), [Zone("z", 600, "WARNING")], auth=make_auth())


def det(sensor_id, key=None, **kw):
    e = {"sensor_id": sensor_id, "ts": time.time(), "azimuth_deg": 45.0, **kw}
    if sensor_id == "radar1":
        e.setdefault("range_m", 500.0)
        e.setdefault("elevation_deg", 5.0)
    if key is not None:
        e["key"] = key
    return e


def test_verify():
    a = make_auth()
    assert a.verify("radar1", KEYS["radar1"])
    assert not a.verify("radar1", KEYS["radar1"] + "x") and not a.verify("radar1", KEYS["radar1"][:-1])
    assert not a.verify("radar1", KEYS["rf1"]), "ключ чужого датчика"
    assert not a.verify("ghost", KEYS["radar1"]), "невідомий датчик"
    for bad in (None, "", 0, 123, [], {}, b"bytes", "k" * 100_000):
        assert a.verify("radar1", bad) is False, repr(bad)[:30]
    assert a.verify("radar1", "\ud800") is False, "самотній суррогат (дозволений JSON-ом) не має кидати виняток"


def test_engine_rejects_events_without_valid_key():
    eng = make_engine()
    for label, ev in {"без ключа": det("rf1"), "хибний ключ": det("rf1", "nope"), "ключ чужого датчика": det("rf1", KEYS["radar1"]),
                      "ключ None": det("rf1", None), "ключ числом": det("rf1", 12345)}.items():
        r = eng.ingest_detailed([ev])
        assert (r.accepted, r.rejected, r.unauthorized) == (0, 1, 1), label
    assert eng.buffer == [] and eng.stats["events_unauthorized"] == 5 and eng.stats["events_in"] == 0

    r = eng.ingest_detailed([det("rf1", KEYS["rf1"]), det("radar1", KEYS["radar1"]), det("ac1", "nope")])
    assert (r.accepted, r.rejected, r.unauthorized) == (2, 1, 1)
    assert eng.ingest([det("rf1", KEYS["rf1"])]) == (1, 0), "ingest() лишився сумісним: (прийнято, відхилено)"


def test_forged_heartbeat_does_not_hide_a_dead_sensor():
    """Без цього атакувальник маскував би відмову радара підробленими heartbeat."""
    eng = make_engine()
    forged = [{"sensor_id": "radar1", "heartbeat": True}, {"sensor_id": "radar1", "heartbeat": True, "key": "nope"}]
    assert eng.ingest(forged) == (0, 2)
    assert eng.sensor_status()["radar1"]["state"] == "WAITING", "підроблений heartbeat зарахувався як ознака життя"
    assert eng.ingest([{"sensor_id": "radar1", "heartbeat": True, "key": KEYS["radar1"]}]) == (1, 0)
    assert eng.sensor_status()["radar1"]["state"] == "OK"


def test_key_is_not_stored_and_disabled_auth_ignores_keys():
    eng = make_engine()
    eng.ingest([det("rf1", KEYS["rf1"])])
    assert KEYS["rf1"] not in repr(eng.buffer) and KEYS["rf1"] not in json.dumps(eng.state())
    open_eng = Engine(default_sensors(), [Zone("z", 600, "WARNING")])        # auth=None: поведінка до автентифікації
    assert open_eng.ingest([det("rf1"), det("rf1", "будь-який")]) == (2, 0)


def test_unauthorized_flood_is_cheap_and_does_not_fill_buffer():
    eng = make_engine()
    r = eng.ingest_detailed([det("rf1", "nope") for _ in range(5000)])
    assert r.unauthorized == 5000 and eng.buf_count["rf1"] == 0, "невалідні події не мають витісняти справжні зі стелі"
    assert eng.ingest([det("rf1", KEYS["rf1"]) for _ in range(10)]) == (10, 0)


def _client():
    try:
        from fastapi.testclient import TestClient
        import app
        import test_health
    except ImportError as e:
        print("skip: service deps missing:", e)
        return None, None
    m = test_health._app_with_engine()
    m.engine.auth = make_auth()
    return TestClient(m.app, raise_server_exceptions=False), m


def test_http_requires_key_per_sensor():
    c, m = _client()
    if c is None:
        return
    ev = lambda sid, **kw: {k: v for k, v in det(sid, **kw).items() if k != "key"}   # у тілі ключа немає: він у заголовку
    h = lambda key: {"X-API-Key": key} if key else {}

    r = c.post("/api/events", json=[ev("rf1")])
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "ApiKey" and "ключ" in r.json()["detail"]
    assert c.post("/api/events", json=[ev("rf1")], headers=h("wrong")).status_code == 401
    assert c.post("/api/events", json=[ev("rf1")], headers=h(KEYS["radar1"])).status_code == 401, "ключ іншого датчика"

    ok = c.post("/api/events", json=[ev("rf1")], headers=h(KEYS["rf1"]))
    assert ok.status_code == 200 and ok.json() == {"accepted": 1, "rejected": 0, "unauthorized": 0}

    mixed = c.post("/api/events", json=[ev("rf1"), ev("ac1")], headers=h(KEYS["rf1"]))   # ключ один, датчиків два
    assert mixed.status_code == 200 and mixed.json() == {"accepted": 1, "rejected": 1, "unauthorized": 1}

    hb = c.post("/api/events", json=[{"sensor_id": "radar1", "heartbeat": True}], headers=h(KEYS["radar1"]))
    assert hb.status_code == 200 and hb.json()["accepted"] == 1
    assert c.post("/api/events", json=[]).status_code == 200, "порожній пакет не є помилкою автентифікації"

    smuggled = ev("rf1"); smuggled["key"] = KEYS["rf1"]      # ключ у тілі ігнорується: лише заголовок
    assert c.post("/api/events", json=[smuggled]).status_code == 401
    assert m.engine.stats["events_unauthorized"] == 5   # без ключа, хибний, чужий, ac1 у змішаному, ключ у тілі


def test_keys_never_appear_in_responses_or_logs():
    c, m = _client()
    if c is None:
        return
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger().addHandler(handler)
    try:
        bad = "SECRET-WRONG-KEY-12345"
        r1 = c.post("/api/events", json=[{"sensor_id": "rf1", "ts": time.time(), "azimuth_deg": 1.0}], headers={"X-API-Key": bad})
        r2 = c.post("/api/events", content='[{"sensor_id":"rf1","azimuth_deg":NaN}]', headers={"X-API-Key": bad, "Content-Type": "application/json"})
        health = c.get("/api/health")
    finally:
        logging.getLogger().removeHandler(handler)
    blob = r1.text + r2.text + health.text + " ".join(r.getMessage() for r in records)
    assert bad not in blob and not any(k in blob for k in KEYS.values())
    assert health.json()["auth"] is True
    assert "key_sha256" not in c.get("/api/config").text, "хеші ключів не мають віддаватись у /api/config"


def test_mqtt_events_carry_key_in_payload():
    try:
        from mqtt_in import MqttIngest
    except ImportError as e:
        print("skip: no paho:", e)
        return
    eng = make_engine()
    ing = MqttIngest(eng, "localhost")
    msg = lambda payload, topic="dronewatch/events/rf1": types.SimpleNamespace(
        payload=payload if isinstance(payload, bytes) else json.dumps(payload).encode(), topic=topic)
    ing._on_message(None, None, msg([det("rf1", KEYS["rf1"]), det("rf1")]))
    assert eng.stats["events_in"] == 1 and eng.stats["events_unauthorized"] == 1
    ing._on_message(None, None, msg(b'[{"sensor_id":"rf1","key":"\\ud800","heartbeat":true}]'))   # самотній суррогат у JSON
    assert eng.stats["events_unauthorized"] == 2


def _load_cfg(yaml_text):
    import config
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        return config.load(p)


LIVE = """
mode: live
{auth}
zones: [{{name: z, radius_m: 100, level: WARNING}}]
sensors:
  - {{id: radar1, type: radar, position: [0, 0], max_range: 1500{key}}}
"""


def test_config_is_fail_closed():
    try:
        import config  # noqa: F401
    except ImportError as e:
        print("skip: no pydantic/yaml:", e)
        return

    def rejects(text, fragment):
        try:
            _load_cfg(text)
        except SystemExit as e:
            assert fragment in str(e), (fragment, str(e))
            return
        raise AssertionError(f"конфіг мав бути відхилений ({fragment})")

    good = hash_key("k")
    rejects(LIVE.format(auth="", key=""), "key_sha256")                                         # за замовчуванням required
    rejects(LIVE.format(auth="auth: {mode: required}", key=""), "key_sha256")
    rejects(LIVE.format(auth="", key=', key_sha256: "' + "0" * 64 + '"'), "заглушка")
    rejects(LIVE.format(auth="", key=', key_sha256: "xyz"'), "SHA-256")
    rejects(LIVE.format(auth="auth: {mode: maybe}", key=""), "mode")
    cfg = _load_cfg(LIVE.format(auth="", key=f', key_sha256: "{good.upper()}"'))
    assert cfg.sensors[0].key_sha256 == good, "hex нормалізується до нижнього регістру"
    assert _load_cfg(LIVE.format(auth="auth: {mode: disabled}", key="")).auth.mode == "disabled"
    assert _load_cfg("mode: demo\n").mode == "demo", "демо без автентифікації стартує як і раніше"
    assert _load_cfg((HERE.parent / "deploy" / "config.yaml").read_text(encoding="utf-8")).sensors[0].key_sha256


def test_deploy_demo_keys_match_compose():
    """Ключі емулятора в docker-compose.yml мають відповідати хешам у deploy/config.yaml, інакше `docker compose up` не запрацює."""
    try:
        import yaml
    except ImportError as e:
        print("skip: no yaml:", e)
        return
    root = HERE.parent
    env = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["emulator"]["environment"]["DRONEWATCH_KEYS"]
    keys = dict(item.split("=", 1) for item in env.split(","))
    cfg = yaml.safe_load((root / "deploy" / "config.yaml").read_text(encoding="utf-8"))
    hashes = {s["id"]: s["key_sha256"] for s in cfg["sensors"]}
    assert set(keys) == set(hashes) and all(hash_key(keys[s]) == hashes[s] for s in hashes)


def test_keygen_roundtrip():
    out = subprocess.run([sys.executable, str(HERE / "keygen.py"), "radar1", "rf1"], capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout
    lines = out.splitlines()
    keys = [l.split(": ")[-1] for l in lines if "ключ датчика" in l]
    hashes = [l.split('"')[1] for l in lines if "key_sha256" in l]
    assert len(keys) == len(hashes) == 2 and keys[0] != keys[1] and all(len(k) >= 40 for k in keys), "ключі випадкові й достатньо довгі"
    a = SensorAuth({"radar1": hashes[0], "rf1": hashes[1]})
    assert a.verify("radar1", keys[0]) and a.verify("rf1", keys[1]) and not a.verify("radar1", keys[1])
    assert subprocess.run([sys.executable, str(HERE / "keygen.py"), "--hash", keys[0]], capture_output=True, text=True,
                          check=True).stdout.strip() == hashes[0]


def test_emulator_key_parsing_never_echoes_a_key():
    import emulate
    assert emulate.parse_keys(["radar1=A"], "rf1=B,ac1=C") == {"rf1": "B", "ac1": "C", "radar1": "A"}
    assert emulate.parse_keys(["radar1=NEW"], "radar1=OLD") == {"radar1": "NEW"}, "прапорець перекриває оточення"
    for bad in (["SECRET-KEY-WITHOUT-NAME"], ["=SECRET"], ["radar1="]):
        try:
            emulate.parse_keys(bad, "")
        except SystemExit as e:
            assert "SECRET" not in str(e.code)
        else:
            raise AssertionError(bad)


class _RC:
    """Схожий на paho.ReasonCode: is_failure і текстове подання."""

    def __init__(self, text, failure):
        self.text, self.is_failure = text, failure

    def __str__(self):
        return self.text


def test_mqtt_topic_must_match_sensor_id():
    """Датчик, якому брокер дозволив лише свій топік, не може видавати події за іншого: навіть з його чинним ключем."""
    try:
        from mqtt_in import MqttIngest
    except ImportError as e:
        print("skip: no paho:", e)
        return
    eng = make_engine()
    ing = MqttIngest(eng, "localhost")
    msg = lambda payload, topic: types.SimpleNamespace(payload=json.dumps(payload).encode(), topic=topic)
    ing._on_message(None, None, msg([det("rf1", KEYS["rf1"])], "dronewatch/events/rf1"))
    assert eng.stats["events_in"] == 1 and eng.stats["events_unauthorized"] == 0
    # rf1 публікує подію radar1 (з вкраденим ключем radar1) у свій топік: рушій навіть не бачить її
    ing._on_message(None, None, msg([det("radar1", KEYS["radar1"])], "dronewatch/events/rf1"))
    assert eng.stats["events_in"] == 1 and eng.stats["events_unauthorized"] == 1
    # змішаний пакет: своя подія проходить, чужа відсікається
    ing._on_message(None, None, msg([det("rf1", KEYS["rf1"]), det("ac1", KEYS["ac1"])], "dronewatch/events/rf1"))
    assert eng.stats["events_in"] == 2 and eng.stats["events_unauthorized"] == 2


def test_mqtt_reports_refused_connection_and_subscription_without_leaking_password():
    try:
        from mqtt_in import MqttIngest
    except ImportError as e:
        print("skip: no paho:", e)
        return
    ing = MqttIngest(make_engine(), "broker", 1883, username="dronewatch-service", password="SUPER-SECRET-PW")
    records, handler = [], logging.Handler()
    handler.emit = records.append
    logging.getLogger("dronewatch").addHandler(handler)
    try:
        for _ in range(5):   # цикл перепідключення б'є тією самою відмовою: у лозі має бути один рядок
            ing._on_connect(None, None, None, _RC("Not authorized", True), None)
        assert ing.status()["connected"] is False
        n_connect = len(records)

        class FakeClient:
            def subscribe(self, *a, **k):
                pass
        ing._on_connect(FakeClient(), None, None, _RC("Success", False), None)
        assert ing.connected and ing.status()["connected"] is False, "з'єднання без прийнятої підписки не вважається робочим"
        for _ in range(3):
            ing._on_subscribe(None, None, 1, [_RC("Not authorized", True)], None)
        assert ing.status()["connected"] is False
        ing._on_subscribe(None, None, 2, [_RC("Granted QoS 0", False)], None)
        assert ing.status()["connected"] is True
    finally:
        logging.getLogger("dronewatch").removeHandler(handler)
    text = " | ".join(r.getMessage() for r in records)
    assert n_connect == 1 and len(records) == 2, [r.getMessage() for r in records]
    assert "SUPER-SECRET-PW" not in text and "Not authorized" in text and "ACL" in text


def test_mqtt_idle_shows_connected_but_silent():
    """Mosquitto приймає й заборонену ACL підписку мовчки, тож єдиний видимий симптом це «зʼєднання є, подій немає»."""
    try:
        from mqtt_in import MqttIngest
    except ImportError as e:
        print("skip: no paho:", e)
        return
    ing = MqttIngest(make_engine(), "broker")
    assert ing.status() == {"host": "broker:1883", "connected": False, "idle_s": None, "tls": False}

    class FakeClient:
        def subscribe(self, *a, **k):
            pass
    ing._on_connect(FakeClient(), None, None, _RC("Success", False), None)
    ing._on_subscribe(None, None, 1, [_RC("Granted QoS 0", False)], None)
    st = ing.status()
    assert st["connected"] and 0 <= st["idle_s"] < 2, st
    ing.since -= 30                                  # 30 с тому підключилися й відтоді жодного повідомлення
    assert 29 < ing.status()["idle_s"] < 32
    msg = types.SimpleNamespace(payload=b"[]", topic="dronewatch/events/rf1")
    ing._on_message(None, None, msg)
    assert ing.status()["idle_s"] < 2, "повідомлення скидає простій"
    ing._on_disconnect(None, None, None, _RC("lost", True), None)
    assert ing.status()["idle_s"] is None
    ing._on_connect(FakeClient(), None, None, _RC("Success", False), None)
    ing._on_subscribe(None, None, 2, [_RC("Granted QoS 0", False)], None)
    assert ing.status()["idle_s"] < 2, "після перепідключення простій рахується від нового підключення, а не від старого повідомлення"


def _pbkdf2(password, line):
    """Перевірка пароля проти рядка $7$101$сіль$хеш незалежно від keygen (власна реалізація формату брокера)."""
    import base64
    import hashlib
    _, _, iters, salt, digest = line.split("$")
    got = hashlib.pbkdf2_hmac("sha512", password.encode(), base64.b64decode(salt), int(iters))
    return base64.b64decode(digest) == got


def test_mosquitto_hash_format_and_cli():
    import keygen
    line = keygen.mosquitto_hash("пароль-123")
    assert line.startswith("$7$101$") and line.count("$") == 4 and _pbkdf2("пароль-123", line) and not _pbkdf2("інший", line)
    assert keygen.mosquitto_hash("x") != keygen.mosquitto_hash("x"), "сіль має бути випадковою"
    run = lambda *args: subprocess.run([sys.executable, str(HERE / "keygen.py"), *args], capture_output=True, text=True,
                                       encoding="utf-8", check=True).stdout
    user, _, rest = run("--mosquitto", "healthcheck", "pw").strip().partition(":")
    assert user == "healthcheck" and _pbkdf2("pw", rest)
    full = run("radar1")
    key = next(l.split(": ")[-1] for l in full.splitlines() if "ключ датчика" in l)
    entry = next(l.split("mosquitto.passwd: ")[1] for l in full.splitlines() if "mosquitto.passwd:" in l)
    assert entry.startswith("radar1:") and _pbkdf2(key, entry.split(":", 1)[1]), "пароль у брокері = той самий ключ датчика"


def test_broker_files_match_demo_stack():
    """Статична перевірка стенда: passwd відповідає паролям у compose, ACL і конфіг брокера закриті, як задумано."""
    try:
        import yaml
    except ImportError as e:
        print("skip: no yaml:", e)
        return
    root = HERE.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    sensor_keys = dict(i.split("=", 1) for i in compose["services"]["emulator"]["environment"]["DRONEWATCH_KEYS"].split(","))
    svc_pw = compose["services"]["service"]["environment"]["MQTT_PASSWORD"]
    hc_pw = " ".join(compose["services"]["mosquitto"]["healthcheck"]["test"]).split("-P ")[1].split()[0]
    passwd = dict(l.split(":", 1) for l in (root / "deploy" / "mosquitto.passwd").read_text(encoding="utf-8").split())

    expected = {**sensor_keys, "dronewatch-service": svc_pw, "healthcheck": hc_pw}
    assert set(passwd) == set(expected), "у passwd рівно датчики, сервіс і healthcheck, ніхто зайвий"
    assert all(_pbkdf2(expected[u], passwd[u]) for u in expected), "хеші в passwd не відповідають паролям у compose"
    assert compose["services"]["service"]["environment"]["MQTT_USER"] == "dronewatch-service"

    active = [l.split("#")[0].strip() for l in (root / "deploy" / "mosquitto.conf").read_text(encoding="utf-8").splitlines()]
    assert "allow_anonymous false" in active and "use_username_as_clientid true" in active
    assert "allow_anonymous true" not in active
    assert "password_file /mosquitto/config/passwd" in active and "acl_file /mosquitto/config/acl" in active

    acl = [l.strip() for l in (root / "deploy" / "mosquitto.acl").read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.strip().startswith("#")]
    assert acl[0] == "pattern write dronewatch/events/%u", "правило для датчиків: писати лише у власний топік"
    assert [l for l in acl if l.startswith("pattern")] == [acl[0]], "інших загальних правил, що діяли б на всіх, бути не має"
    per_user, cur = {}, None
    for l in acl[1:]:
        if l.startswith("user "):
            cur = l.split()[1]
            per_user[cur] = []
        else:
            per_user[cur].append(l)
    assert per_user == {"dronewatch-service": ["topic read dronewatch/events/#"], "healthcheck": ["topic read $SYS/broker/uptime"]}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
