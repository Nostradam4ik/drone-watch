"""Ядро сервісу: приймає події датчиків, веде треки, оцінює загрози, пише інциденти.

Формат події (однаковий для будь-якого джерела: HTTP, WebSocket, у майбутньому MQTT):
    {"sensor_id": "radar1", "ts": 1760000000.5,
     "azimuth_deg": 212.0,           # компасний пеленг: від півночі за годинниковою стрілкою
     "range_m": 640.0,               # лише для радара
     "elevation_deg": 4.5}           # лише для радара

Heartbeat (рекомендовано): {"sensor_id": "radar1", "heartbeat": true}. Датчик шле його кожен скан, навіть коли нічого
не бачить. Без нього «порожнє небо» і «датчик помер» не відрізнити, тож тиша довше за sensor_timeout_s
вважається відмовою (безпечний бік: тривоги без цього датчика лишаються, а не зникають).
"""
import math
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import numpy as np

from auth import SensorAuth
from models import Floats, Sensor
from tracker import Tracker

Level = Literal["NONE", "WARNING", "CRITICAL"]
SensorState = Literal["OK", "WAITING", "LOST"]
LEVELS: dict[Level, int] = {"NONE": 0, "WARNING": 1, "CRITICAL": 2}
STALE_S = 2.0          # події старші за це відкидаються; події з майбутнього (розбіжність годинників) теж, понад цей допуск
RANGE_SLACK = 1.5      # радар може трохи перебрати max_range (шум, повільне згасання), але не вдвічі
OK, WAITING, LOST = "OK", "WAITING", "LOST"   # стан здоровʼя датчика
Measurement = tuple[str, Floats]               # (датчик, вимірювання у внутрішньому форматі трекера)


class Alert(TypedDict):
    ts: float
    track_id: int
    level: Level
    zone: str
    pos: list[float]
    speed: float
    sensors: list[str]


class SensorStatus(TypedDict):
    id: str
    label: str
    state: SensorState
    age_s: float | None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """accepted: прийняті детекції й heartbeat; rejected: усе відхилене (невалідне, понад стелю, без ключа);
    unauthorized: частина rejected."""
    accepted: int
    rejected: int
    unauthorized: int


@dataclass(slots=True)
class _Screened:
    """Підсумок перевірки пакета подій (без замка): що можна класти в буфер і що відхилено та чому."""
    beats: list[str] = field(default_factory=list)
    parsed: list[Measurement] = field(default_factory=list)
    invalid: int = 0
    dropped: int = 0
    denied: int = 0


def compass_to_math(deg: float) -> float:
    return math.radians(90.0 - deg)


def math_to_compass(rad: float) -> float:
    return (90.0 - math.degrees(rad)) % 360.0


@dataclass(eq=False)
class Zone:
    name: str
    radius: float
    level: Level
    center: Any = (0.0, 0.0)   # приймає кортеж чи масив; зберігається як масив

    def __post_init__(self) -> None:
        self.center = np.asarray(self.center, float)

    def contains(self, pos: Floats) -> bool:
        return bool(np.linalg.norm(pos[:2] - self.center) <= self.radius)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "radius": self.radius, "level": self.level, "center": self.center.tolist()}


class Engine:
    def __init__(self, sensors: Iterable[Sensor], zones: list[Zone], db_path: str = ":memory:", scan_dt: float = 0.5,
                 min_age_s: float = 5.0, require_radar: bool = True, max_coast: int = 2, sensor_timeout_s: float = 5.0,
                 clock: Callable[[], float] = time.time, max_events_per_sensor: int = 150, max_tracks: int = 200,
                 auth: SensorAuth | None = None) -> None:
        # min_age_s: тривога лише для треку, підтвердженого щонайменше стільки секунд (відсів короткоживучих хибних)
        # require_radar: у зоні радара тривога лише з радарним підтвердженням; max_coast: скільки сканів без детекцій трек ще «живий»
        self.min_age_s, self.require_radar, self.max_coast = min_age_s, require_radar, max_coast
        self.sensor_timeout_s, self.clock = sensor_timeout_s, clock   # clock підміняється в тестах
        self.auth = auth   # SensorAuth або None (автентифікація вимкнена: демо, довірена мережа)
        # Стелі проти потопу подій: понад них події відкидаються й рахуються, але скан лишається в межах бюджету часу.
        self.max_events_per_sensor, self.max_tracks = max_events_per_sensor, max_tracks
        sensors = list(sensors)
        self.sensors: dict[str, Sensor] = {s.name: s for s in sensors}
        self.zones = zones
        self.scan_dt = scan_dt
        self.tracker = Tracker(sensors, max_tracks=max_tracks)
        self.t0 = self.started = self.clock()
        self.last_seen: dict[str, float] = {}   # sensor_id -> час, коли від датчика востаннє щось прийшло (подія або heartbeat)
        self.heard: set[str] = set()            # датчики, що озивалися з попереднього скану
        self.buffer: list[Measurement] = []
        self.buf_count: Counter[str] = Counter()   # подій у буфері від кожного датчика (для стелі)
        # Один замок на весь змінюваний стан: його беруть і HTTP-потоки (ingest, state), і потік MQTT, і цикл сканів.
        self.lock = threading.Lock()
        self.max_level: dict[int, Level] = {}   # track_id -> найвищий рівень, що вже дав тривогу
        self.alerts: list[Alert] = []           # в памʼяті останні; повний журнал у SQLite
        self.stats: dict[str, int] = dict.fromkeys(
            ("events_in", "events_rejected", "events_unauthorized", "events_dropped", "heartbeats", "scans",
             "scan_errors", "tracks_capped", "bearings_truncated"), 0)
        self.calibration: dict[str, Any] = {"done": False, "note": "не виконано"}
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, track_id INTEGER, level TEXT, zone TEXT,
            x REAL, y REAL, z REAL, speed REAL, sensors TEXT)""")
        self.db.commit()

    # --- вхід
    def reset(self) -> None:
        """Новий сеанс трекінгу (журнал інцидентів лишається)."""
        with self.lock:
            self.tracker = Tracker(list(self.sensors.values()), max_tracks=self.max_tracks)
            self.buffer.clear()
            self.buf_count.clear()
            self.max_level.clear()
            self.t0 = self.clock()

    def count(self, key: str, n: int = 1) -> None:
        """Потокобезпечний лічильник: `stats[k] += n` із кількох потоків (HTTP, MQTT) губить приріст."""
        with self.lock:
            self.stats[key] += n

    def ingest(self, events: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
        """Те саме, що ingest_detailed, але повертає (прийнято, відхилено): для старих викликів і тестів."""
        r = self.ingest_detailed(events)
        return r.accepted, r.rejected

    def ingest_detailed(self, events: Iterable[Mapping[str, Any]]) -> IngestResult:
        """Автентифікує, валідує події й кладе у буфер до наступного скану.
        Автентифікація тут, в одному місці, для всіх джерел (HTTP, MQTT): подія без чинного ключа свого датчика
        відкидається ДО будь-чого іншого й не зараховується навіть як heartbeat. Ключ лежить у event["key"] і в буфер не потрапляє.
        Відхилені = невалідні + відкинуті стелею + без ключа (окремо вони в stats).
        Перевірка йде поза замком (`_screen`), у буфер і лічильники пакет потрапляє одним захопленням замка (`_commit`)."""
        now = self.clock()
        return self._commit(self._screen(events, now), now)

    def _screen(self, events: Iterable[Mapping[str, Any]], now: float) -> _Screened:
        out = _Screened()
        taken: Counter[str] = Counter()   # підказка без замка: не валідуємо більше за стелю від одного датчика в пакеті
        for e in events:
            sid = e.get("sensor_id")
            if not isinstance(sid, str):   # з MQTT може прийти що завгодно, зокрема нехешований список: це не повинно валити ingest
                out.invalid += 1
            elif self.auth is not None and not self.auth.verify(sid, e.get("key")):
                out.denied += 1
            elif e.get("heartbeat"):
                if sid in self.sensors:
                    out.beats.append(sid)
                else:
                    out.invalid += 1
            elif taken[sid] >= self.max_events_per_sensor:
                out.dropped += 1
            elif (z := self._to_internal(sid, e, now)) is None:
                out.invalid += 1
            else:
                taken[sid] += 1
                out.parsed.append(z)
        return out

    def _commit(self, screened: _Screened, now: float) -> IngestResult:
        dropped, accepted = screened.dropped, 0
        with self.lock:
            for sid in screened.beats:
                self._mark(sid, now)
            for z in screened.parsed:
                if self.buf_count[z[0]] >= self.max_events_per_sensor:   # остаточна перевірка: буфер спільний між потоками
                    dropped += 1
                    continue
                self.buf_count[z[0]] += 1
                self.buffer.append(z)
                self._mark(z[0], now)
                accepted += 1
            self.stats["events_in"] += accepted
            self.stats["heartbeats"] += len(screened.beats)
            self.stats["events_rejected"] += screened.invalid
            self.stats["events_dropped"] += dropped
            self.stats["events_unauthorized"] += screened.denied
        return IngestResult(accepted + len(screened.beats), screened.invalid + dropped + screened.denied, screened.denied)

    def _mark(self, sensor_id: str, now: float) -> None:
        """Датчик озвався. Викликати лише під self.lock."""
        self.last_seen[sensor_id] = now
        self.heard.add(sensor_id)

    def _to_internal(self, sensor_id: str, e: Mapping[str, Any], now: float) -> Measurement | None:
        """Подія -> (датчик, вимірювання) або None. Вхід недовірений (HTTP, MQTT): NaN/Infinity у JSON, нефізичні значення
        й мітки часу з майбутнього відхиляються тут, а не доходять до фільтра (один NaN робив трек з NaN у стані)."""
        s = self.sensors.get(sensor_id)
        if s is None:
            return None
        try:
            ts, az_deg = float(e["ts"]), float(e["azimuth_deg"])
            if not (math.isfinite(ts) and math.isfinite(az_deg)) or not -STALE_S <= now - ts <= STALE_S:
                return None
            az = compass_to_math(az_deg)
            if s.kind == "radar":
                rng, el_deg = float(e["range_m"]), float(e["elevation_deg"])
                if not (math.isfinite(rng) and math.isfinite(el_deg)) or not 0 < rng <= s.max_range * RANGE_SLACK or abs(el_deg) > 90:
                    return None
                z = np.array([rng, az, math.radians(el_deg)])
            else:
                z = np.array([az])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        angle = 1 if s.kind == "radar" else 0   # позиція азимута у векторі вимірювання
        z[angle] = (z[angle] + math.pi) % (2 * math.pi) - math.pi
        return (s.name, z)

    # --- скан
    def tick(self) -> list[Alert]:
        with self.lock:
            dets, self.buffer = self.buffer, []
            self.buf_count.clear()
            heard, self.heard = self.heard, set()
            t = self.clock() - self.t0
            down = {sid for sid, st in self._sensor_status().items() if st["state"] == LOST}
            self.tracker.step(t, dets, heard=heard, down=down)
            for k, v in self.tracker.dropped.items():
                self.stats[k] += v
            self.stats["scans"] += 1
            return self._evaluate(t)

    def _highest_zone(self, pos: Floats) -> Zone | None:
        """Зона найвищого рівня, що містить точку (за рівних перемагає перша в списку); None, якщо точка поза зонами."""
        best: Zone | None = None
        for z in self.zones:
            if z.contains(pos) and (best is None or LEVELS[z.level] > LEVELS[best.level]):
                best = z
        return best

    def _evaluate(self, t: float) -> list[Alert]:
        new_alerts: list[Alert] = []
        for tr in self.tracker.confirmed():
            if tr.confirmed_at is None or tr.misses > self.max_coast or t - tr.confirmed_at < self.min_age_s:
                continue
            if self.require_radar and self.tracker._in_radar_cover(tr) and t - tr.kinds["radar"] > 3.0:
                continue  # у зоні радара тривогу без радарного підтвердження не піднімаємо
            zone = self._highest_zone(tr.x[:3])
            if zone is not None and LEVELS[zone.level] > LEVELS[self.max_level.get(tr.id, "NONE")]:
                self.max_level[tr.id] = zone.level
                new_alerts.append({"ts": self.clock(), "track_id": tr.id, "level": zone.level, "zone": zone.name,
                                   "pos": [round(float(v), 1) for v in tr.x[:3]],
                                   "speed": round(float(np.linalg.norm(tr.x[3:5])), 1),
                                   "sensors": sorted(k for k, ts in tr.kinds.items() if t - ts < 3.0)})
        for a in new_alerts:
            self.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (?,?,?,?,?,?,?,?,?)",
                            (a["ts"], a["track_id"], a["level"], a["zone"], *a["pos"], a["speed"], ",".join(a["sensors"])))
        if new_alerts:
            self.db.commit()
            self.alerts = (new_alerts[::-1] + self.alerts)[:200]
        alive = {tr.id for tr in self.tracker.tracks}
        for tid in list(self.max_level):
            if tid not in alive:
                del self.max_level[tid]
        return new_alerts

    # --- вихід
    def sensor_status(self) -> dict[str, SensorStatus]:
        with self.lock:
            return self._sensor_status()

    def _sensor_status(self) -> dict[str, SensorStatus]:
        """Стан кожного датчика (під self.lock): OK (озивався щойно), WAITING (ще не озивався, але час очікування не вийшов),
        LOST (мовчить довше за sensor_timeout_s). Відмову видно за мовчанням, а не за самими детекціями."""
        now = self.clock()
        out: dict[str, SensorStatus] = {}
        for sid, s in self.sensors.items():
            seen = self.last_seen.get(sid)
            age = now - (seen if seen is not None else self.started)
            out[sid] = {"id": sid, "label": s.label, "state": self._health(seen, age), "age_s": round(age, 1) if seen is not None else None}
        return out

    def _health(self, seen: float | None, age: float) -> SensorState:
        if age > self.sensor_timeout_s:
            return "LOST"
        return "OK" if seen is not None else "WAITING"

    def degraded(self) -> list[str]:
        return [sid for sid, st in self.sensor_status().items() if st["state"] == LOST]

    def state(self, truth: Mapping[str, Floats] | None = None) -> dict[str, Any]:
        # треки змінюються в tick() з іншого потоку: без замка /api/state міг би прочитати напіноновлений стан
        with self.lock:
            return self._state(truth)

    def _state(self, truth: Mapping[str, Floats] | None) -> dict[str, Any]:
        t = self.clock() - self.t0
        tracks = []
        for tr in self.tracker.confirmed():
            zone = self._highest_zone(tr.x[:3])
            tracks.append({
                "id": tr.id, "x": round(float(tr.x[0]), 1), "y": round(float(tr.x[1]), 1), "z": round(float(tr.x[2]), 1),
                "vx": round(float(tr.x[3]), 1), "vy": round(float(tr.x[4]), 1),
                "speed": round(float(np.linalg.norm(tr.x[3:5])), 1),
                "err": round(float(math.sqrt(max(tr.P[0, 0] + tr.P[1, 1], 0))), 1),
                "score": round(float(tr.score(t)), 2),
                "kinds": sorted(k for k, ts in tr.kinds.items() if t - ts < 3.0),
                "coasting": tr.misses > self.max_coast, "level": zone.level if zone else "NONE",
            })
        out: dict[str, Any] = {
            "t": round(t, 1), "tracks": tracks, "stats": dict(self.stats), "calibration": self.calibration,
            "sensors": list(self._sensor_status().values()),
            "tentative": sum(1 for tr in self.tracker.tracks if tr.state == "TENTATIVE")}
        if truth is not None:
            out["truth"] = {k: [round(float(v), 1) for v in p[:3]] for k, p in truth.items()}
        return out

    def config(self) -> dict[str, Any]:
        return {
            "sensors": [{"id": s.name, "kind": s.kind, "label": s.label, "pos": s.pos[:2].tolist(), "max_range": s.max_range}
                        for s in self.sensors.values()],
            "zones": [z.as_dict() for z in self.zones],
            "scan_dt": self.scan_dt,
        }
