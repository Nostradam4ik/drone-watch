"""Завантаження і перевірка config.yaml: датчики, зони, параметри тривог."""
import math
from pathlib import Path
from typing import Any, Literal, Self

import numpy as np
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from dronewatch.fusion.models import Sensor
from dronewatch.service.dashboard_auth import Role, check_redis_url, parse_hash
from dronewatch.service.engine import Zone


class Noise(BaseModel):
    """Похибка вимірювання (1σ). Для пеленгових датчиків потрібен лише azimuth_deg."""
    range_m: float = Field(default=5.0, gt=0)
    azimuth_deg: float = Field(default=1.0, gt=0)
    elevation_deg: float = Field(default=1.5, gt=0)


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
    password_hash: str = Field(description="scrypt-хеш пароля; видає dronewatch-keygen --dashboard-user ІМ'Я --role РОЛЬ")
    role: Role = Field(description="viewer читає, operator ще вирішує тривоги й питає AI, admin ще керує розкладкою, покриттям і журналом")

    @field_validator("password_hash")
    @classmethod
    def _valid_hash(cls, v: str) -> str:
        try:
            parse_hash(v)
        except ValueError as e:
            raise ValueError(f"{e}. Згенеруйте: dronewatch-keygen --dashboard-user ІМ'Я --role РОЛЬ") from None
        return v


class DashboardCfg(BaseModel):
    """required: дашборд, API читання й WebSocket лише після входу (за замовчуванням у live). disabled: явна відмова
    (локальна розробка, довірена мережа); сервіс пише про це попередження. У demo вхід обов'язковий, якщо задано users;
    без users demo лише для перегляду. Без входу анонім завжди має лише роль viewer."""
    auth: Literal["required", "disabled"] = "required"
    users: list[DashboardUser] = []
    session_ttl_s: float = Field(default=8 * 3600, gt=0, le=30 * 86400)   # скільки діє сесія після входу
    session_store: Literal["memory", "redis"] = Field(default="memory", description="redis: сесії й ліміт входів спільні для процесів")
    redis_url: str | None = Field(default=None, description="rediss://... (TLS) або redis://localhost; пароль краще в DRONEWATCH_REDIS_URL")

    @model_validator(mode="after")
    def _redis_ok(self) -> "DashboardCfg":
        if self.redis_url is not None:
            check_redis_url(self.redis_url)
        return self


class SensorCfg(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["radar", "rf", "acoustic"]
    position: list[float] = Field(description="[east_m, north_m] відносно початку координат об'єкта")
    max_range: float = Field(gt=0)
    heading_deg: float = Field(default=0.0, ge=0, lt=360, description="куди дивиться датчик, компасні градуси (північ = 0); для секторних")
    fov_deg: float = Field(default=360.0, gt=0, le=360, description="ширина сектора огляду по азимуту; 360 = круговий огляд")
    noise: Noise = Noise()
    latency_s: float = Field(default=0.0, ge=0)
    bias: Bias = Bias()
    key_sha256: str | None = Field(default=None, description="SHA-256 ключа датчика (hex); ключ видає dronewatch-keygen <id>")

    @field_validator("key_sha256")
    @classmethod
    def _key_hash(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("потрібен SHA-256 у hex (64 символи), у лапках; згенеруйте: dronewatch-keygen <id>")
        if set(v) == {"0"}:
            raise ValueError("це заглушка з прикладу, жоден ключ їй не відповідає; згенеруйте справжній: dronewatch-keygen <id>")
        return v

    @field_validator("position")
    @classmethod
    def _pos(cls, v: list[float]) -> list[float]:
        if len(v) not in (2, 3):
            raise ValueError("position: потрібно [east, north] або [east, north, up]")
        return v


class ZoneCfg(BaseModel):
    name: str
    radius_m: float = Field(gt=0)
    level: Literal["WARNING", "CRITICAL"]
    center: list[float] = [0.0, 0.0]


class AlertsCfg(BaseModel):
    min_age_s: float = Field(default=5.0, ge=0)               # скільки секунд трек має бути підтвердженим до тривоги
    require_radar_in_cover: bool = True               # у зоні радара тривога лише з радарним підтвердженням
    max_coast_scans: int = Field(default=2, ge=0)


class AiCfg(BaseModel):
    """AI-асистент оператора (RAG + tool-calling чат). enabled: true лише вмикає намір; якщо MISTRAL_API_KEY
    не задано в оточенні, сервіс все одно стартує, а /api/ai/ask чесно каже, що AI недоступний."""
    enabled: bool = False
    model: str = "mistral-small-latest"
    embedding_model: str = "mistral-embed"
    qdrant_url: str = "http://localhost:6333"
    max_tool_iterations: int = Field(default=4, ge=1, le=10)


class MqttCfg(BaseModel):
    host: str
    port: int = Field(default=1883, ge=1, le=65535)   # з TLS брокер зазвичай слухає 8883
    topic: str = "dronewatch/events/#"
    username: str | None = None
    password: str | None = None
    tls: bool = False                         # шифрувати зʼєднання; сертифікат і ім'я хоста перевіряються завжди
    ca_cert: str | None = None                # довіряти лише цьому CA (PEM); задання ca_cert вмикає TLS


class OriginCfg(BaseModel):
    """Де на карті світу лежить точка (0, 0) системи координат датчиків (схід, північ у метрах). Лише для показу на карті."""
    lat: float = Field(default=48.8584, ge=-90, le=90)    # за замовчуванням Ейфелева вежа
    lon: float = Field(default=2.2945, ge=-180, le=180)


class MapCfg(BaseModel):
    """Підкладка карти дашборда. За замовчуванням ЛИШЕ власний файл (dronewatch-mapbuild): тайли з чужого сервера видають
    йому, яку ділянку розглядає оператор, тому онлайн-тайли вмикаються свідомо (online_tiles: true) і лише без файлу."""
    pmtiles: str = "data/map/paris.pmtiles"   # шлях відносно робочої теки сервісу
    online_tiles: bool = False


class Config(BaseModel):
    mode: Literal["demo", "live"] = "demo"
    scan_dt: float = Field(default=0.5, gt=0)
    # Стелі проти потопу подій. Вартість скану ~ (треки x події), тож вибирайте за бюджетом часу: вищі значення дозволяють
    # більші рої, але під атакою потопом скан довшає. Понад стелі події відкидаються й видно в stats / на дашборді.
    max_events_per_sensor_scan: int = Field(default=150, ge=1)
    max_tracks: int = Field(default=200, ge=1)
    sensor_timeout_s: float = Field(default=5.0, gt=0)   # тиша датчика довше за це = відмова (потрібен heartbeat від датчика)
    sensors: list[SensorCfg] = []
    zones: list[ZoneCfg] = []
    origin: OriginCfg = OriginCfg()
    map: MapCfg = MapCfg()
    alerts: AlertsCfg = AlertsCfg()
    mqtt: MqttCfg | None = None
    auth: AuthCfg = AuthCfg()
    dashboard: DashboardCfg = DashboardCfg()
    ai: AiCfg = AiCfg()

    @model_validator(mode="after")
    def _check(self) -> Self:
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
                                     "(dronewatch-keygen --dashboard-user ІМ'Я --role РОЛЬ) "
                                     "або свідомо вимкніть: dashboard: {auth: disabled}")
                if len(names) != len(set(names)):
                    raise ValueError("імена користувачів дашборда мають бути унікальними")
            if self.auth.mode == "required":
                keyless = [s.id for s in self.sensors if not s.key_sha256]
                if keyless:
                    raise ValueError(f"немає key_sha256 для датчиків {keyless}: автентифікація обовʼязкова. Згенеруйте ключі "
                                     f"(dronewatch-keygen {' '.join(keyless)}) або свідомо вимкніть: auth: {{mode: disabled}}")
        return self


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config(**raw)
    except FileNotFoundError:
        raise SystemExit(f"Конфіг не знайдено: {path}") from None
    except yaml.YAMLError as e:
        raise SystemExit(f"Помилка YAML у {path}:\n{e}") from None
    except ValidationError as e:
        lines = [f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise SystemExit(f"Помилки в конфігу {path}:\n" + "\n".join(lines)) from None
    except ValueError as e:
        raise SystemExit(f"Помилка в конфігу {path}: {e}") from None


def build_sensors(cfg: Config) -> list[Sensor]:
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
        s.fov = math.radians(sc.fov_deg)
        s.heading = math.radians(90.0 - sc.heading_deg)   # компас -> математичний кут
        sensors.append(s)
    return sensors


def build_zones(cfg: Config) -> list[Zone]:
    return [Zone(z.name, z.radius_m, z.level, tuple(z.center)) for z in cfg.zones]


def calibration_note(cfg: Config) -> dict[str, Any]:
    """Чи є в конфігу калібрування: показується на дашборді."""
    if any(any(v != 0 for v in s.bias.model_dump().values()) for s in cfg.sensors):
        return {"done": True, "note": "bias із конфігу"}
    return {"done": False, "note": "не виконано"}
