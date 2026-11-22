"""Розстановка датчиків із дашборда (лише demo): перевірка того, що прислав клієнт, і побудова датчиків для симулятора та рушія."""
from typing import Any, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dronewatch.fusion.models import Sensor
from dronewatch.fusion.sim import SENSOR_TYPES, make_sensor
from dronewatch.service.engine import Zone

MAX_SENSORS = 12     # стеля: кожен датчик це додаткові виміри в кожному скані
LIMIT_M = 5000.0     # як далеко від початку координат можна ставити (симуляція цілей і так працює в межах ~1 км)


class PlacedSensor(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")   # NaN/Infinity і зайві поля: 422
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
    type: str
    x: float = Field(ge=-LIMIT_M, le=LIMIT_M)   # схід, м
    y: float = Field(ge=-LIMIT_M, le=LIMIT_M)   # північ, м
    heading: float = Field(default=0.0, ge=0.0, lt=360.0)   # куди дивиться, компасні градуси; важливо лише для секторних типів

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in SENSOR_TYPES:
            raise ValueError("невідомий тип датчика: оберіть із " + ", ".join(SENSOR_TYPES))
        return v


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensors: list[PlacedSensor] = Field(max_length=MAX_SENSORS)

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [s.id for s in self.sensors]
        if len(ids) != len(set(ids)):
            raise ValueError("id датчиків мають бути унікальними")
        radars = sum(SENSOR_TYPES[s.type]["kind"] == "radar" for s in self.sensors)
        if not radars and len(self.sensors) - radars < 2:
            raise ValueError("потрібен щонайменше один радар або два пеленгові датчики (RF чи акустичні): "
                             "інакше трекеру нема з чого визначити позицію")
        return self


def build(layout: Layout) -> list[Sensor]:
    return [make_sensor(s.type, s.id, s.x, s.y, s.heading) for s in layout.sensors]


# Охоронювані зони виводяться з радарів: навколо кожного радара буферна (увага) і критична (загроза) зона, радіус пропорційний
# дальності радара. Для радара на 1500 м це 600 і 250 м, як у вбудованому конфігу демо.
BUFFER_FRAC = 0.4
CRITICAL_FRAC = 1 / 6
FALLBACK_RADII = (600.0, 250.0)   # без радарів (лише пеленгові датчики): зони навколо центра датчиків


def derive_zones(sensors: list[Sensor]) -> list[Zone]:
    radars = [s for s in sensors if s.kind == "radar"]
    if radars:
        return [z for s in radars for z in (Zone(f"Буферна зона {s.name}", round(s.max_range * BUFFER_FRAC, 1), "WARNING", s.pos[:2]),
                                             Zone(f"Критична зона {s.name}", round(s.max_range * CRITICAL_FRAC, 1), "CRITICAL", s.pos[:2]))]
    centre = np.mean([s.pos[:2] for s in sensors], axis=0) if sensors else (0.0, 0.0)
    return [Zone("Буферна зона", FALLBACK_RADII[0], "WARNING", centre), Zone("Критична зона", FALLBACK_RADII[1], "CRITICAL", centre)]


def type_catalog() -> list[dict[str, Any]]:
    """Палітра для дашборда: що можна поставити й чим типи відрізняються."""
    return [{"id": k, "name": v["name"], "label": v["label"], "max_range": v["max_range"], "sigma": list(v["sigma"]), "pd": v["pd"],
             "fov_deg": v.get("fov_deg", 360.0), "real": v.get("real")} for k, v in SENSOR_TYPES.items()]
