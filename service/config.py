"""Завантаження і перевірка config.yaml: датчики, зони, параметри тривог."""
import math
import sys
from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fusion_proto"))
from models import Sensor  # noqa: E402

from dashboard_auth import parse_hash  # noqa: E402
from engine import Zone  # noqa: E402


class Noise(BaseModel):
    """Похибка вимірювання (1σ). Для пеленгових датчиків потрібен лише azimuth_deg."""
    range_m: float = Field(5.0, gt=0)
    azimuth_deg: float = Field(1.0, gt=0)
    elevation_deg: float = Field(1.5, gt=0)


class Bias(BaseModel):
    """Відома систематична похибка (вимірювання мінус істина) з калібрування. Азимут компасний."""
    range_m: float = 0.0
    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0


class AuthCfg(BaseModel):
    """required: події без чинного ключа свого датчика відкидаються (за замовчуванням у live). disabled: явна відмова
    від автентифікації (довірена ізольована мережа); сервіс пише про це попередження при старті."""
    mode: Literal["required", "disabled"] = "required"


class DashboardUser(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.@-]{1,64}$")
    password_hash: str = Field(description="scrypt-хеш пароля; видає python keygen.py --dashboard-user ІМ'Я")

    @field_validator("password_hash")
    @classmethod
    def _valid_hash(cls, v: str) -> str:
        try:
            parse_hash(v)
        except ValueError as e:
            raise ValueError(f"{e}. Згенеруйте: python keygen.py --dashboard-user ІМ'Я") from None
        return v


class DashboardCfg(BaseModel):
    """required: дашборд, API читання й WebSocket лише після входу (за замовчуванням у live). disabled: явна відмова
    (локальна розробка, довірена мережа); сервіс пише про це попередження. У demo автентифікація вимкнена завжди."""
    auth: Literal["required", "disabled"] = "required"
    users: list[DashboardUser] = []
    session_ttl_s: float = Field(8 * 3600, gt=0, le=30 * 86400)   # скільки діє сесія після входу


class SensorCfg(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["radar", "rf", "acoustic"]
    position: list[float] = Field(description="[east_m, north_m] відносно початку координат об'єкта")
    max_range: float = Field(gt=0)
    noise: Noise = Noise()
    latency_s: float = Field(0.0, ge=0)
    bias: Bias = Bias()
    key_sha256: str | None = Field(None, description="SHA-256 ключа датчика (hex); ключ видає python keygen.py <id>")

    @field_validator("key_sha256")
    @classmethod
    def _key_hash(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("потрібен SHA-256 у hex (64 символи), у лапках; згенеруйте: python keygen.py <id>")
        if set(v) == {"0"}:
            raise ValueError("це заглушка з прикладу, жоден ключ їй не відповідає; згенеруйте справжній: python keygen.py <id>")
        return v

    @field_validator("position")
    @classmethod
    def _pos(cls, v):
        if len(v) not in (2, 3):
            raise ValueError("position: потрібно [east, north] або [east, north, up]")
        return v


class ZoneCfg(BaseModel):
    name: str
    radius_m: float = Field(gt=0)
    level: Literal["WARNING", "CRITICAL"]
    center: list[float] = [0.0, 0.0]


class AlertsCfg(BaseModel):
    min_age_s: float = Field(5.0, ge=0)               # скільки секунд трек має бути підтвердженим до тривоги
    require_radar_in_cover: bool = True               # у зоні радара тривога лише з радарним підтвердженням
    max_coast_scans: int = Field(2, ge=0)


class MqttCfg(BaseModel):
    host: str
    port: int = Field(1883, ge=1, le=65535)   # з TLS брокер зазвичай слухає 8883
    topic: str = "dronewatch/events/#"
    username: str | None = None
    password: str | None = None
    tls: bool = False                         # шифрувати зʼєднання; сертифікат і ім'я хоста перевіряються завжди
    ca_cert: str | None = None                # довіряти лише цьому CA (PEM); задання ca_cert вмикає TLS


class Config(BaseModel):
    mode: Literal["demo", "live"] = "demo"
    scan_dt: float = Field(0.5, gt=0)
    # Стелі проти потопу подій. Вартість скану ~ (треки x події), тож вибирайте за бюджетом часу: вищі значення дозволяють
    # більші рої, але під атакою потопом скан довшає. Понад стелі події відкидаються й видно в stats / на дашборді.
    max_events_per_sensor_scan: int = Field(150, ge=1)
    max_tracks: int = Field(200, ge=1)
    sensor_timeout_s: float = Field(5.0, gt=0)   # тиша датчика довше за це = відмова (потрібен heartbeat від датчика)
    sensors: list[SensorCfg] = []
    zones: list[ZoneCfg] = []
    alerts: AlertsCfg = AlertsCfg()
    mqtt: MqttCfg | None = None
    auth: AuthCfg = AuthCfg()
    dashboard: DashboardCfg = DashboardCfg()

    @model_validator(mode="after")
    def _check(self):
        ids = [s.id for s in self.sensors]
        if len(ids) != len(set(ids)):
            raise ValueError("id датчиків мають бути унікальними")
        if self.mode == "live":
            radars = [s for s in self.sensors if s.type == "radar"]
            bearings = [s for s in self.sensors if s.type != "radar"]
            if not radars and len(bearings) < 2:
                raise ValueError("для live потрібен щонайменше один радар або два пеленгові датчики (RF/акустика): "
                                 "інакше трекеру нема з чого визначити позицію")
            if not self.zones:
                raise ValueError("не описано жодної зони: тривоги не буде за чим оцінювати")
            if self.dashboard.auth == "required":
                names = [u.name for u in self.dashboard.users]
                if not names:
                    raise ValueError("немає жодного користувача дашборда: вхід обовʼязковий. Додайте dashboard.users "
                                     "(python keygen.py --dashboard-user ІМ'Я) або свідомо вимкніть: dashboard: {auth: disabled}")
                if len(names) != len(set(names)):
                    raise ValueError("імена користувачів дашборда мають бути унікальними")
            if self.auth.mode == "required":
                keyless = [s.id for s in self.sensors if not s.key_sha256]
                if keyless:
                    raise ValueError(f"немає key_sha256 для датчиків {keyless}: автентифікація обовʼязкова. Згенеруйте ключі "
                                     f"(python keygen.py {' '.join(keyless)}) або свідомо вимкніть: auth: {{mode: disabled}}")
        return self


def load(path):
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config(**raw)
    except FileNotFoundError:
        raise SystemExit(f"Конфіг не знайдено: {path}")
    except yaml.YAMLError as e:
        raise SystemExit(f"Помилка YAML у {path}:\n{e}")
    except ValidationError as e:
        lines = [f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise SystemExit(f"Помилки в конфігу {path}:\n" + "\n".join(lines))
    except ValueError as e:
        raise SystemExit(f"Помилка в конфігу {path}: {e}")


def build_sensors(cfg):
    sensors = []
    for sc in cfg.sensors:
        pos = list(sc.position) + [0.0] * (3 - len(sc.position))
        if sc.type == "radar":
            s = Sensor(sc.id, "radar", pos, (sc.noise.range_m, math.radians(sc.noise.azimuth_deg), math.radians(sc.noise.elevation_deg)),
                       0.9, sc.max_range, 0.0, "radar")
            # bias_est віднімається від вимірювання; компасний азимут = 90° − математичний, тож знак азимута змінюється
            s.bias_est = np.array([sc.bias.range_m, -math.radians(sc.bias.azimuth_deg), math.radians(sc.bias.elevation_deg)])
        else:
            s = Sensor(sc.id, "bearing", pos, (math.radians(sc.noise.azimuth_deg),), 0.7, sc.max_range, 0.0, sc.type)
            s.bias_est = np.array([-math.radians(sc.bias.azimuth_deg)])
        s.latency = sc.latency_s
        s.latency_comp = sc.latency_s   # затримку вважаємо відомою (синхронізація годинників)
        sensors.append(s)
    return sensors


def build_zones(cfg):
    return [Zone(z.name, z.radius_m, z.level, tuple(z.center)) for z in cfg.zones]


def calibration_note(cfg):
    """Чи є в конфігу калібрування: показується на дашборді."""
    if any(any(v != 0 for v in s.bias.model_dump().values()) for s in cfg.sensors):
        return {"done": True, "note": "bias із конфігу"}
    return {"done": False, "note": "не виконано"}
