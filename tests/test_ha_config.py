"""Конфіг `ha`: вимкнений за замовчуванням, а ввімкнений вимагає Redis і режим live."""
import pytest
from pydantic import ValidationError

from dronewatch.service.config import Config, HaCfg

LIVE = {
    "mode": "live",
    "sensors": [{"id": "r1", "type": "radar", "position": [0, 0], "max_range": 1000}],
    "zones": [{"name": "z", "radius_m": 500, "level": "CRITICAL", "center": [0, 0]}],
    "auth": {"mode": "disabled"},
    "dashboard": {"auth": "disabled"},
}


def test_ha_off_by_default():
    assert Config().ha.enabled is False
    assert HaCfg().lease_ttl_s == 6.0 and HaCfg().instance_id == "auto"


def test_ha_enabled_needs_redis_url():
    with pytest.raises(ValidationError, match=r"ha.redis_url"):
        Config(**LIVE, ha={"enabled": True})


def test_ha_requires_live_mode():
    with pytest.raises(ValidationError, match="mode: live"):
        Config(mode="demo", ha={"enabled": True, "redis_url": "redis://x"})


def test_ha_enabled_ok_in_live():
    cfg = Config(**LIVE, ha={"enabled": True, "redis_url": "redis://x"})
    assert cfg.ha.enabled


@pytest.mark.parametrize("ttl", [1.0, 61.0])
def test_ttl_bounds(ttl):
    with pytest.raises(ValidationError):
        HaCfg(lease_ttl_s=ttl)
