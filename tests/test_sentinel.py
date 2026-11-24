"""Redis Sentinel для сесій, ліміту входів і релея: конфіг, TLS, fail-closed, перепідключення після зміни master."""
import threading
import time

import pytest
from pydantic import ValidationError

from dronewatch.service import app, config, dashboard_redis
from dronewatch.service.dashboard_auth import SessionStoreError
from dronewatch.service.ha import RedisEventRelay
from test_ha_lease import FakeRedis
from test_redis_sessions import FAST

SENTINELS = ["a.internal:26379", "b.internal:26379", "w.internal:26379"]
LIVE = {
    "mode": "live",
    "sensors": [{"id": "r1", "type": "radar", "position": [0, 0], "max_range": 1000}],
    "zones": [{"name": "z", "radius_m": 500, "level": "CRITICAL", "center": [0, 0]}],
    "auth": {"mode": "disabled"},
    "dashboard": {"auth": "disabled"},
}


def test_redis_section_off_by_default():
    assert config.Config().redis is None


@pytest.mark.parametrize("sentinels", [
    ["a:26379", "b:26379"],                       # менше трьох: кворум 2 з 3 неможливий
    ["a:26379", "b:26379", "c"],                  # без порту
    ["a:26379", "b:26379", "c:notaport"],
    ["a:26379", "b:26379", "c:70000"],
])
def test_bad_sentinels_rejected(sentinels):
    with pytest.raises(ValidationError):
        config.Config(redis={"sentinels": sentinels})


def test_sentinel_replaces_redis_url_for_ha():
    cfg = config.Config(**LIVE, ha={"enabled": True}, redis={"sentinels": SENTINELS, "master_name": "dw"})
    assert cfg.redis is not None and cfg.redis.master_name == "dw"


def test_ha_without_url_or_sentinel_still_rejected():
    with pytest.raises(ValidationError, match=r"ha\.redis_url"):
        config.Config(**LIVE, ha={"enabled": True})


def test_connect_sentinel_passes_tls_and_secrets(monkeypatch):
    redis = pytest.importorskip("redis")
    seen: dict = {}

    class FakeMaster:
        def ping(self):
            return True

    class FakeSentinel:
        def __init__(self, sentinels, **kw):
            seen["sentinels"], seen["kw"] = sentinels, kw

        def master_for(self, name, **kw):
            seen["name"], seen["master_kw"] = name, kw
            return FakeMaster()

    monkeypatch.setattr("redis.sentinel.Sentinel", FakeSentinel)
    client = dashboard_redis.connect_sentinel([("a", 26379), ("b", 26379), ("c", 26379)], "dw", "s3cret", "/ca.pem")
    assert isinstance(client, FakeMaster) and redis
    assert seen["sentinels"] == [("a", 26379), ("b", 26379), ("c", 26379)] and seen["name"] == "dw"
    kw, mkw = seen["kw"], seen["master_kw"]
    assert kw["ssl"] is True and kw["ssl_ca_certs"] == "/ca.pem" and kw["password"] == "s3cret"
    assert kw["sentinel_kwargs"]["ssl"] is True and kw["sentinel_kwargs"]["password"] == "s3cret"
    assert mkw["decode_responses"] is True and mkw["socket_timeout"] == dashboard_redis.TIMEOUT_S


def test_connect_sentinel_unreachable_is_session_store_error_without_secrets():
    pytest.importorskip("redis")
    with pytest.raises(SessionStoreError) as e:
        dashboard_redis.connect_sentinel([("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.1", 3)], "dw", "s3cretpw", None)
    text = str(e.value) + repr(e.value) + repr(e.value.__cause__) + repr(e.value.__context__)
    assert "s3cretpw" not in text and "127.0.0.1" not in text


def test_missing_redis_package_message(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("redis"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(SessionStoreError, match=r"dronewatch\[redis\]"):
        dashboard_redis.connect_sentinel([("a", 1), ("b", 2), ("c", 3)], "dw", None, None)


def test_build_runtime_ha_uses_sentinel_not_url(monkeypatch, tmp_path):
    fake = FakeRedis()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.delenv("MQTT_HOST", raising=False)
    got: dict = {}

    def sentinel(sentinels, master_name, password, ca):
        got.update(sentinels=sentinels, master_name=master_name, password=password, ca=ca)
        return fake

    monkeypatch.setattr(dashboard_redis, "connect_sentinel", sentinel)
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: pytest.fail("звичайний connect не мав викликатись"))
    monkeypatch.setenv("DRONEWATCH_REDIS_PASSWORD", "pw")
    cfg = config.Config(**LIVE, ha={"enabled": True}, redis={"sentinels": SENTINELS, "master_name": "dw", "ca_cert": "/ca.pem"})
    rt = app.build_runtime(cfg)
    assert rt.ha is not None
    assert got == {"sentinels": [("a.internal", 26379), ("b.internal", 26379), ("w.internal", 26379)], "master_name": "dw",
                   "password": "pw", "ca": "/ca.pem"}


def test_dashboard_sessions_use_sentinel(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(dashboard_redis, "connect_sentinel", lambda *a: fake)
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: pytest.fail("звичайний connect не мав викликатись"))
    cfg = config.Config(
        redis={"sentinels": SENTINELS},
        dashboard=config.DashboardCfg(session_store="redis",
                                      users=[config.DashboardUser(name="a", role="admin", password_hash=FAST)]))
    access = app.build_dashboard_access(cfg)
    assert isinstance(access.sessions, dashboard_redis.RedisSessionStore)


class DyingPubSub:
    """Підписка до master, що помер: get_message падає, як у справжнього клієнта при обриві з'єднання."""

    def __init__(self, redis: "FailoverRedis") -> None:
        self.redis, self.queue = redis, []

    def subscribe(self, channel):
        pass

    def get_message(self, timeout=0.0):
        if self.redis.master_dead:
            raise ConnectionError("master помер")
        return self.queue.pop(0) if self.queue else None

    def close(self):
        pass


class FailoverRedis:
    def __init__(self) -> None:
        self.master_dead = False
        self.subs: list[DyingPubSub] = []
        self.pubsubs_created = 0

    def pubsub(self, ignore_subscribe_messages=False):
        if self.master_dead:
            raise ConnectionError("master помер")
        self.pubsubs_created += 1
        ps = DyingPubSub(self)
        self.subs.append(ps)
        return ps

    def publish(self, channel, message):
        for ps in self.subs:
            ps.queue.append({"type": "message", "channel": channel, "data": message})


def test_relay_resubscribes_after_master_change():
    """Після перемикання master підписка рветься (ConnectionError) і відновлюється сама; події після цього доходять."""
    r = FailoverRedis()
    relay = RedisEventRelay(r)
    got: list[list[dict]] = []
    stop = threading.Event()
    t = threading.Thread(target=relay.subscribe, args=(got.append, stop), daemon=True)
    t.start()
    time.sleep(0.2)
    assert r.pubsubs_created == 1
    r.master_dead = True
    time.sleep(0.3)
    r.master_dead = False   # Sentinel обрав новий master
    r.subs.clear()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not got:
        relay.publish([{"sensor": "r1"}])
        time.sleep(0.1)
    stop.set()
    t.join(2)
    assert r.pubsubs_created == 2 and got and got[0] == [{"sensor": "r1"}]
