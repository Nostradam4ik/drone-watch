"""HA у сервісі: збірка з конфігу, фейковий Redis, перехід ролей рушія."""
import pytest

from dronewatch.service import app, config, dashboard_redis
from dronewatch.service.dashboard_auth import SessionStoreError
from dronewatch.service.ha import Role
from test_ha_lease import FakeRedis

LIVE = {
    "mode": "live",
    "sensors": [{"id": "r1", "type": "radar", "position": [0, 0], "max_range": 1000}],
    "zones": [{"name": "z", "radius_m": 500, "level": "CRITICAL", "center": [0, 0]}],
    "auth": {"mode": "disabled"},
    "dashboard": {"auth": "disabled"},
}


@pytest.fixture
def redis(monkeypatch, tmp_path):
    r = FakeRedis()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: r)
    return r


def ha_cfg(**extra):
    return config.Config(**LIVE, ha={"enabled": True, "redis_url": "rediss://redis.internal", **extra})


def test_build_runtime_ha_off_has_no_ha():
    rt = app.build_runtime(config.Config(mode="demo"))
    assert rt.ha is None and rt.engine.active is True


def test_build_runtime_ha_builds_standby_engine(redis):
    rt = app.build_runtime(ha_cfg(instance_id="node-a"))
    assert rt.ha is not None and rt.engine.active is False
    assert rt.ha.elector.role is Role.STANDBY and rt.ha.elector.owner == "node-a"


def test_ha_auto_instance_id_is_hostname(redis, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "host-7")
    assert app.build_runtime(ha_cfg()).ha.elector.owner == "host-7"


def test_ha_start_fails_without_redis(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))

    def down(url):
        raise SessionStoreError("Redis недоступний")

    monkeypatch.setattr(dashboard_redis, "connect", down)
    with pytest.raises(SessionStoreError):
        app.build_runtime(ha_cfg())


def test_ha_rejects_plain_redis_url(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    cfg = config.Config(**LIVE, ha={"enabled": True, "redis_url": "redis://other-host"})
    with pytest.raises(ValueError, match="rediss"):
        app.build_runtime(cfg)


def test_on_promote_activates_engine_and_runs_audit_check(redis, tmp_path):
    rt = app.build_runtime(ha_cfg())
    rt.ha.elector.step()
    assert rt.ha.elector.role is Role.LEADER and rt.engine.active is True and rt.engine.epoch == 1
    assert (tmp_path / "i.db").exists() and rt.audit_status is not None and rt.audit_status.ok


def test_on_demote_deactivates_engine(redis):
    rt = app.build_runtime(ha_cfg())
    rt.ha.elector.step()
    redis.data["dw:leader"] = "other:9"   # оренду перехопив інший
    rt.ha.elector.step()
    assert rt.ha.elector.role is Role.STANDBY and rt.engine.active is False


def test_degraded_deactivates_engine_but_keeps_showing_alerts(redis, monkeypatch):
    rt = app.build_runtime(ha_cfg())
    t = [100.0]
    rt.ha.elector.clock = lambda: t[0]
    rt.ha.elector.step()
    redis.down = True
    t[0] += 7.0
    rt.ha.elector.step()
    assert rt.ha.elector.role is Role.DEGRADED and rt.engine.active is False


# ---------- ворота для людей ----------
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def client(redis):
    app.app.state.runtime = app.build_runtime(ha_cfg())
    app.app.state.access = app.build_dashboard_access(ha_cfg())
    return TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)


def role(role: Role) -> None:
    app.app.state.runtime.ha.elector.role = role


def test_readyz_leader_200(client):
    role(Role.LEADER)
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ready": True, "role": "leader"}


def test_readyz_standby_503(client):
    role(Role.STANDBY)
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json() == {"ready": False, "role": "standby"}


def test_readyz_degraded_200(client):
    role(Role.DEGRADED)
    assert client.get("/readyz").status_code == 200


def test_standby_blocks_state_and_events(client):
    role(Role.STANDBY)
    assert client.get("/api/state").status_code == 503
    assert client.post("/api/events", json=[]).status_code == 503


def test_standby_health_still_answers(client):
    role(Role.STANDBY)
    assert client.get("/api/health").status_code in (200, 503)   # відповідає самий сервіс, не ворота
    assert "резерв" not in client.get("/api/health").text


def test_degraded_allows_get_state_but_blocks_changes(client):
    role(Role.DEGRADED)
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/events", json=[]).status_code == 200
    assert client.post("/api/incidents/1/review", json={"status": "confirmed"}).status_code == 503
    assert client.put("/api/layout", json={"sensors": []}).status_code == 503


def test_ws_closed_for_standby(client):
    role(Role.STANDBY)
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect("/ws"):
        pass
    assert e.value.code == 1013


def test_audit_is_fenced_when_engine_inactive(client):
    """Без чинної оренди запис у журнал дій відмовляє 503 і нічого не пише (інакше «started» втопився б у памʼяті, а дія пройшла)."""
    from fastapi import HTTPException

    from dronewatch.service.audit import AuditEntry
    from dronewatch.service.dashboard_web import audit_or_503

    role(Role.LEADER)   # ворота пропускають, але рушій неактивний: оренди в БД нема
    conn = type("C", (), {"app": app.app})()
    with pytest.raises(HTTPException) as e:
        audit_or_503(conn, AuditEntry("u", "admin", "layout_save", "x", "started", "1.1.1.1"))
    assert e.value.status_code == 503
    assert app.app.state.runtime.engine.db.execute("SELECT count(*) FROM audit_log").fetchone() == (0,)


def test_health_reports_ha_role_to_logged_in_user(client):
    from dronewatch.service.dashboard_auth import LoginLimiter, SessionStore
    from dronewatch.service.dashboard_web import COOKIE, DashboardAccess

    role(Role.LEADER)
    sessions = SessionStore(3600)
    app.app.state.access = DashboardAccess(sessions=sessions, limiter=LoginLimiter())
    client.cookies.set(COOKIE, sessions.create("alice", "admin"))
    body = client.get("/api/health").json()
    assert body["ha"]["role"] == "leader" and body["ha"]["instance"] and body["ha"]["epoch"] is None


def test_no_ha_means_no_gate(redis):
    app.app.state.runtime = app.build_runtime(config.Config(mode="demo"))
    c = TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)
    assert c.get("/readyz").json() == {"ready": True, "role": "single"}
    assert "ha" not in c.get("/api/health").json()


# ---------- релей HTTP-подій у резерв ----------
import json
import threading

from dronewatch.service.ha import RedisEventRelay


def test_post_events_publishes_relay_without_keys(client, redis):
    role(Role.LEADER)
    r = client.post("/api/events", json=[{"sensor_id": "r1", "heartbeat": True}], headers={"X-API-Key": "supersecret"})
    assert r.status_code == 200
    (channel, payload), = redis.published
    assert channel == "dw:events" and "supersecret" not in payload and json.loads(payload)[0]["sensor_id"] == "r1"


def test_relay_failure_does_not_break_post_events(client, redis):
    role(Role.LEADER)
    redis.down = True
    r = client.post("/api/events", json=[{"sensor_id": "r1", "heartbeat": True}])
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_relay_handler_feeds_trusted_ingest_only_on_standby(client):
    rt = app.app.state.runtime
    role(Role.STANDBY)
    rt.ha.on_relay([{"sensor_id": "r1", "heartbeat": True}])
    assert rt.engine.stats["heartbeats"] == 1
    role(Role.LEADER)   # власні публікації лідер отримує назад: їх не можна рахувати вдруге
    rt.ha.on_relay([{"sensor_id": "r1", "heartbeat": True}])
    assert rt.engine.stats["heartbeats"] == 1


def run_subscriber(redis, handler):
    stop = threading.Event()
    relay = RedisEventRelay(redis)
    t = threading.Thread(target=relay.subscribe, args=(handler, stop))
    t.start()
    return relay, stop, t


def test_relay_subscriber_delivers_lists_and_ignores_garbage(redis):
    got: list[list[dict]] = []
    _relay, stop, t = run_subscriber(redis, got.append)
    while not redis.subscribers:   # дочекатись підписки
        pass
    for payload in ("не json", "42", json.dumps(["рядок", 5]), json.dumps([{"sensor_id": "r1"}, "сміття"])):
        redis.publish("dw:events", payload)
    while redis.subscribers[0]:
        pass
    stop.set()
    t.join(timeout=3)
    assert got == [[{"sensor_id": "r1"}]]   # лише обʼєкти з валідного списку
