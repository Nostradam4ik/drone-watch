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
