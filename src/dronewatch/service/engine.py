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
import contextlib
import math
import sqlite3
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import numpy as np

from dronewatch.fusion.models import Floats, Sensor
from dronewatch.fusion.tracker import Tracker
from dronewatch.service.audit import AuditEntry, AuditError, AuditLog
from dronewatch.service.auth import SensorAuth
from dronewatch.service.catalog import DRONES, UNKNOWN_MODEL, describe

Level = Literal["NONE", "WARNING", "CRITICAL"]
SensorState = Literal["OK", "WAITING", "LOST"]
LEVELS: dict[Level, int] = {"NONE": 0, "WARNING": 1, "CRITICAL": 2}
STALE_S = 2.0          # події старші за це відкидаються; події з майбутнього (розбіжність годинників) теж, понад цей допуск
IDENT_GATE = math.radians(15)    # RF-ідентифікація привʼязується до треку, якщо пеленг збігається з ним у межах цього кута
IDENT_AMBIGUITY = math.radians(4)   # два треки майже на одному пеленгу: невідомо, чия це модель, тож ідентифікацію не чіпаємо
MAX_IDENTS_PER_SCAN = 500
DIVE_VZ = 4.0                    # м/с: так швидко має знижуватись ціль, щоб це вважалось пікіруванням, а не шумом висоти
DIVE_ANGLE = math.radians(10)    # і кут пікірування не менший за цей (відносно горизонту)
DIVE_SCANS = 5                   # стільки сканів поспіль (шум одного скану не рахується)
DIVE_DROP_M = 20.0               # і за вікно нижче висота справді впала щонайменше на стільки метрів (оцінка швидкості сама по собі шумить)
DIVE_WINDOW_S = 4.0
DIVE_RADAR_S = 1.5                # трек має мати радарне підтвердження не старіше за стільки секунд
ALT_RADAR_S = 5.0                 # висота показується як виміряна, поки останній радарний вимір не старший за це (інакше UI: «—»)
DIVE_MIN_AGE_S = 6.0             # трек молодший за це не оцінюємо: у щойно створеного треку швидкість ще не зійшлась і стрибає
MAX_MODEL_LEN = 40
DEFAULT_ORIGIN = (48.8584, 2.2945)   # Ейфелева вежа: куди на карті світу ставимо (0, 0) системи координат
RANGE_SLACK = 1.5      # радар може трохи перебрати max_range (шум, повільне згасання), але не вдвічі
OK, WAITING, LOST = "OK", "WAITING", "LOST"   # стан здоровʼя датчика
Measurement = tuple[str, Floats]               # (датчик, вимірювання у внутрішньому форматі трекера)


VALID_REVIEW_STATUSES: tuple[str, ...] = ("new", "confirmed", "dismissed")
MAX_REVIEW_NOTE = 500


class Alert(TypedDict):
    ts: float
    track_id: int
    level: Level
    zone: str
    pos: list[float]
    speed: float
    sensors: list[str]
    model: str | None        # модель за RF-ідентифікацією (None, якщо невідома)
    model_name: str | None
    side: str                # friend | enemy | unknown
    id: int                  # rowid у журналі incidents
    status: str              # new | confirmed | dismissed (рішення оператора; нічого не пригнічує)
    reviewed_by: str | None
    reviewed_at: float | None
    review_note: str | None


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
    idents: dict[int, str] = field(default_factory=dict)   # індекс у parsed -> ключ моделі (гине разом зі своїм вимірюванням)
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
                 auth: SensorAuth | None = None, origin: tuple[float, float] = DEFAULT_ORIGIN) -> None:
        # min_age_s: тривога лише для треку, підтвердженого щонайменше стільки секунд (відсів короткоживучих хибних)
        # require_radar: у зоні радара тривога лише з радарним підтвердженням; max_coast: скільки сканів без детекцій трек ще «живий»
        self.min_age_s, self.require_radar, self.max_coast = min_age_s, require_radar, max_coast
        self.sensor_timeout_s, self.clock = sensor_timeout_s, clock   # clock підміняється в тестах
        self.origin = origin   # (lat, lon) точки (0, 0): лише для показу на карті, фільтр працює в метрах
        self.auth = auth   # SensorAuth або None (автентифікація вимкнена: демо, довірена мережа)
        # Стелі проти потопу подій: понад них події відкидаються й рахуються, але скан лишається в межах бюджету часу.
        self.max_events_per_sensor, self.max_tracks = max_events_per_sensor, max_tracks
        sensors = list(sensors)
        self.sensors: dict[str, Sensor] = {s.name: s for s in sensors}
        self.zones = zones
        self.scan_dt = scan_dt
        self.tracker = Tracker(sensors, max_tracks=max_tracks)
        self.t0 = self.started = self.clock()
        self.layout_rev = 0   # зростає при кожній зміні розстановки: дашборд за ним помічає, що конфіг застарів
        self.since: dict[str, float] = {}   # sensor_id -> коли датчик додано (для тих, що додані після старту)
        self.last_seen: dict[str, float] = {}   # sensor_id -> час, коли від датчика востаннє щось прийшло (подія або heartbeat)
        self.heard: set[str] = set()            # датчики, що озивалися з попереднього скану
        self.buffer: list[Measurement] = []
        self.buf_count: Counter[str] = Counter()   # подій у буфері від кожного датчика (для стелі)
        self.ident_buffer: list[tuple[str, float, str]] = []
        # track_id -> датчик -> голоси за модель. Один датчик дає один голос на трек за скан, а ключі моделей обмежені каталогом,
        # тож памʼять тут обмежена числом треків x датчиків x розміром каталогу
        self.idents: dict[int, dict[str, Counter[str]]] = {}
        self.dive_scans: dict[int, int] = {}   # track_id -> скільки сканів поспіль ціль пікірує
        self.z_hist: dict[int, deque[tuple[float, float]]] = {}   # track_id -> (час, висота) за останні секунди
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
        have = {r[1] for r in self.db.execute("PRAGMA table_info(incidents)")}
        for col, typ in (("model", "TEXT"), ("model_name", "TEXT"), ("side", "TEXT"), ("status", "TEXT"),
                         ("reviewed_by", "TEXT"), ("reviewed_at", "REAL"), ("review_note", "TEXT")):
            if col not in have:   # міграція старих журналів: нові колонки лишаються NULL у старих рядках (NULL status = new)
                self.db.execute(f"ALTER TABLE incidents ADD COLUMN {col} {typ}")
        self.db.execute("""CREATE TABLE IF NOT EXISTS incident_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, status TEXT, by TEXT, ts REAL, note TEXT)""")
        self.db.commit()
        self.audit = AuditLog(self.db, self.lock)   # той самий файл БД; append бере self.lock, тож під замком лише append_locked

    # --- вхід
    def reset(self) -> None:
        """Новий сеанс трекінгу (журнал інцидентів лишається)."""
        with self.lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        # номери треків тягнемо далі: тривоги в списку й журналі не мають злитися з новою ціллю під тим самим номером
        self.tracker = Tracker(list(self.sensors.values()), max_tracks=self.max_tracks, first_id=self.tracker.next_id)
        self.buffer.clear()
        self.buf_count.clear()
        self.ident_buffer.clear()
        self.idents.clear()
        self.dive_scans.clear()
        self.z_hist.clear()
        self.max_level.clear()
        self.t0 = self.clock()

    def set_sensors(self, sensors: Iterable[Sensor], zones: list[Zone] | None = None) -> None:
        """Нова розстановка датчиків (демо), за потреби разом із новими зонами. Старі треки й рівні тривог втрачають сенс,
        тож це ще й новий сеанс трекінгу. Датчик, якого щойно додали, «очікується», а не «втрачений»:
        відлік його мовчання йде від моменту додавання."""
        sensors = list(sensors)
        with self.lock:
            if zones is not None:
                self.zones = zones
            now = self.clock()
            self.sensors = {s.name: s for s in sensors}   # підміна цілим словником: _screen читає його поза замком
            self.since = {s.name: self.since.get(s.name, now) for s in sensors}
            self.last_seen = {k: v for k, v in self.last_seen.items() if k in self.sensors}
            self.heard &= set(self.sensors)
            self.layout_rev += 1
            self._reset_locked()

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
                model = e.get("drone_model")
                # лише RF-датчик розпізнає протокол дрона; модель від радара чи мікрофона ігноруємо, а не довіряємо
                if isinstance(model, str) and 0 < len(model) <= MAX_MODEL_LEN and self.sensors[sid].label == "rf":
                    out.idents[len(out.parsed) - 1] = model if model in DRONES else UNKNOWN_MODEL
        return out

    def _commit(self, screened: _Screened, now: float) -> IngestResult:
        dropped, accepted = screened.dropped, 0
        with self.lock:
            for sid in screened.beats:
                self._mark(sid, now)
            for i, z in enumerate(screened.parsed):
                if self.buf_count[z[0]] >= self.max_events_per_sensor:   # остаточна перевірка: буфер спільний між потоками
                    dropped += 1
                    continue
                self.buf_count[z[0]] += 1
                self.buffer.append(z)
                model = screened.idents.get(i)
                # лише для прийнятого вимірювання, тобто під тією ж стелею датчика
                if model is not None and len(self.ident_buffer) < MAX_IDENTS_PER_SCAN:
                    self.ident_buffer.append((z[0], float(z[1][0]), model))
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
            self._assign_idents()
            self._update_dives(t)
            for k, v in self.tracker.dropped.items():
                self.stats[k] += v
            self.stats["scans"] += 1
            return self._evaluate(t)

    def _assign_idents(self) -> None:
        """Привʼязує RF-ідентифікації (модель дрона) до треків за пеленгом: береться трек із найближчим пеленгом від того
        RF-датчика, але лише якщо він у межах IDENT_GATE і однозначно ближчий за решту. Під self.lock.
        Один датчик дає не більше одного голосу на трек за скан; якщо за скан він назвав для треку різні моделі, голосу немає."""
        idents, self.ident_buffer = self.ident_buffer, []
        tracks = self.tracker.tracks
        scan: dict[tuple[int, str], set[str]] = {}
        for sid, az, model in idents:
            pos = self.sensors[sid].pos
            diffs = sorted(
                (abs((math.atan2(tr.x[1] - pos[1], tr.x[0] - pos[0]) - az + math.pi) % (2 * math.pi) - math.pi), tr.id)
                for tr in tracks)
            if not diffs or diffs[0][0] > IDENT_GATE:
                continue
            if len(diffs) > 1 and diffs[1][0] - diffs[0][0] < IDENT_AMBIGUITY:
                continue
            scan.setdefault((diffs[0][1], sid), set()).add(model)
        for (tid, sid), models in scan.items():
            if len(models) == 1:
                self.idents.setdefault(tid, {}).setdefault(sid, Counter())[next(iter(models))] += 1

    @staticmethod
    def _is_diving(vx: float, vy: float, vz: float, z: float) -> bool:
        return z > 0 and vz <= -DIVE_VZ and math.atan2(-vz, math.hypot(vx, vy)) >= DIVE_ANGLE

    def _update_dives(self, t: float) -> None:
        """Рахує, скільки сканів поспіль кожна ціль пікірує (стрімко й круто знижується). Під self.lock."""
        alive = set()
        for tr in self.tracker.tracks:
            alive.add(tr.id)
            hist = self.z_hist.setdefault(tr.id, deque())
            hist.append((t, float(tr.x[2])))
            while hist and t - hist[0][0] > DIVE_WINDOW_S + 1.0:
                hist.popleft()
            old = next((z for ts, z in hist if t - ts <= DIVE_WINDOW_S), None)   # найдавніша висота у вікні
            dropped = old is not None and t - hist[0][0] >= DIVE_WINDOW_S - 1.0 and old - float(tr.x[2]) >= DIVE_DROP_M
            mature = tr.confirmed_at is not None and t - tr.confirmed_at >= DIVE_MIN_AGE_S
            if t - tr.kinds["radar"] > DIVE_RADAR_S:   # висоту надійно міряє лише радар; з пеленгів вона блукає, і це не пікірування
                mature = False
            if mature and dropped and self._is_diving(float(tr.x[3]), float(tr.x[4]), float(tr.x[5]), float(tr.x[2])):
                self.dive_scans[tr.id] = self.dive_scans.get(tr.id, 0) + 1
            else:
                self.dive_scans.pop(tr.id, None)
        for tid in set(self.dive_scans) - alive:
            del self.dive_scans[tid]
        for tid in set(self.z_hist) - alive:
            del self.z_hist[tid]

    def _identity(self, track_id: int) -> dict[str, Any]:
        """Що відомо про тип цілі: модель, чия вона й звідки це відомо. Без RF-ідентифікації model = None.
        Якщо різні RF-датчики називають різні моделі, це суперечність: «свій/ворог» не вгадуємо, а позначаємо її явно."""
        none = {"model": None, "model_name": None, "side": "unknown", "country": None, "drone_kind": None, "shape": None,
                "identified_by": None, "identity_conflict": False}
        per_sensor = {sid: c.most_common(1)[0][0] for sid, c in self.idents.get(track_id, {}).items() if c}
        models = set(per_sensor.values())
        if not models:
            return none
        if len(models) > 1:
            return {**none, "identity_conflict": True}
        model = next(iter(models))
        info = describe(model)
        assert info is not None
        return {"model": model, "model_name": info["name"], "side": info["side"], "country": info["country"],
                "drone_kind": info["kind"], "shape": info["shape"], "identified_by": "rf", "identity_conflict": False}

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
            if self.require_radar and self.tracker.in_radar_cover(tr) and t - tr.kinds["radar"] > 3.0:
                continue  # у зоні радара тривогу без радарного підтвердження не піднімаємо
            zone = self._highest_zone(tr.x[:3])
            if zone is not None and LEVELS[zone.level] > LEVELS[self.max_level.get(tr.id, "NONE")]:
                self.max_level[tr.id] = zone.level
                ident = self._identity(tr.id)
                new_alerts.append({"ts": self.clock(), "track_id": tr.id, "level": zone.level, "zone": zone.name,
                                   "pos": [round(float(v), 1) for v in tr.x[:3]],
                                   "speed": round(float(np.linalg.norm(tr.x[3:5])), 1),
                                   "sensors": sorted(k for k, ts in tr.kinds.items() if t - ts < 3.0),
                                   "model": ident["model"], "model_name": ident["model_name"], "side": ident["side"],
                                   "id": 0, "status": "new", "reviewed_by": None, "reviewed_at": None, "review_note": None})
        for a in new_alerts:
            cur = self.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors,"
                                  " model, model_name, side, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'new')",
                                  (a["ts"], a["track_id"], a["level"], a["zone"], *a["pos"], a["speed"], ",".join(a["sensors"]),
                                   a["model"], a["model_name"], a["side"]))
            a["id"] = cur.lastrowid or 0
        if new_alerts:
            self.db.commit()
            self.alerts = (new_alerts[::-1] + self.alerts)[:200]
        alive = {tr.id for tr in self.tracker.tracks}
        for tid in list(self.max_level):
            if tid not in alive:
                del self.max_level[tid]
        for tid in list(self.idents):
            if tid not in alive:
                del self.idents[tid]
        return new_alerts

    # --- рішення оператора
    def review_incident(self, incident_id: int, status: str, by: str, note: str | None = None, *,
                        audit: AuditEntry | None = None) -> dict[str, Any] | None:
        """Записує рішення оператора (і в історію, і в сам рядок журналу). None, якщо такого інциденту нема.
        Нічого не пригнічує: max_level, трекер і зони не чіпаються. Якщо передано `audit`, рядок журналу дій іде в тій самій
        транзакції: збій відкочує все, памʼять не змінюється, піднімається AuditError (рішення без сліду не буває)."""
        if status not in VALID_REVIEW_STATUSES:
            raise ValueError(f"недопустимий статус: {status}")
        clean = (note or "").strip()[:MAX_REVIEW_NOTE] or None
        with self.lock:
            if self.db.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone() is None:
                return None
            ts = self.clock()
            try:
                self.db.execute("INSERT INTO incident_reviews (incident_id, status, by, ts, note) VALUES (?,?,?,?,?)",
                                (incident_id, status, by, ts, clean))
                self.db.execute("UPDATE incidents SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                                (status, by, ts, clean, incident_id))
                if audit is not None:
                    self.audit.append_locked(audit)
                self.db.commit()
            except (sqlite3.Error, AuditError) as e:
                if audit is None:
                    raise
                with contextlib.suppress(sqlite3.Error):
                    self.db.rollback()
                raise AuditError(f"рішення не записано, журнал дій недоступний: {e}") from e
            for a in self.alerts:
                if a["id"] == incident_id:
                    a["status"], a["reviewed_by"], a["reviewed_at"], a["review_note"] = status, by, ts, clean
            cur = self.db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, cur.fetchone(), strict=True))

    def incident_stats(self, since_ts: float | None = None) -> list[dict[str, Any]]:
        """Кількість інцидентів за зоною й статусом (NULL = new); since_ts відсікає старіші."""
        with self.lock:
            rows = self.db.execute("SELECT zone, COALESCE(status,'new'), count(*) FROM incidents WHERE ts >= ? "
                                   "GROUP BY zone, COALESCE(status,'new') ORDER BY zone, 2",
                                   (since_ts if since_ts is not None else float("-inf"),)).fetchall()
        return [{"zone": z, "status": st, "count": n} for z, st, n in rows]

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
            age = now - (seen if seen is not None else self.since.get(sid, self.started))
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
            ident = self._identity(tr.id)
            # висоту міряє лише радар; трек із самих пеленгів має припущені tracker.ASSUMED_ALT, а не виміряні метри
            alt_known = t - tr.kinds["radar"] <= ALT_RADAR_S
            zone = self._highest_zone(tr.x[:3])   # рівень не залежить від side: мітка «свій» приходить з неавтентифікованого RF
            tracks.append({
                "id": tr.id, "x": round(float(tr.x[0]), 1), "y": round(float(tr.x[1]), 1), "z": round(float(tr.x[2]), 1),
                "vx": round(float(tr.x[3]), 1), "vy": round(float(tr.x[4]), 1),
                "speed": round(float(np.linalg.norm(tr.x[3:5])), 1),
                "err": round(float(math.sqrt(max(tr.P[0, 0] + tr.P[1, 1], 0))), 1),
                "score": round(float(tr.score(t)), 2),
                "kinds": sorted(k for k, ts in tr.kinds.items() if t - ts < 3.0),
                "coasting": tr.misses > self.max_coast, "level": zone.level if zone else "NONE",
                "vz": round(float(tr.x[5]), 1),
                "diving": self.dive_scans.get(tr.id, 0) >= DIVE_SCANS,
                # скільки секунд до землі за поточної швидкості зниження (лише коли ціль знижується)
                "ttg": round(float(tr.x[2]) / -float(tr.x[5]), 1) if alt_known and tr.x[5] < -0.5 and tr.x[2] > 0 else None,
                "alt_known": alt_known,
                **ident,
            })
        out: dict[str, Any] = {
            "t": round(t, 1), "tracks": tracks, "stats": dict(self.stats), "calibration": self.calibration,
            "sensors": list(self._sensor_status().values()), "layout_rev": self.layout_rev,
            "tentative": sum(1 for tr in self.tracker.tracks if tr.state == "TENTATIVE")}
        if truth is not None:
            out["truth"] = {k: [round(float(v), 1) for v in p[:3]] for k, p in truth.items()}
        return out

    @staticmethod
    def _sigma_display(s: Sensor) -> list[float]:
        """Похибка датчика для людини: радар (дальність м, азимут °, кут місця °), пеленговий (азимут °)."""
        if s.kind == "radar":
            return [round(float(s.sigma[0]), 2), round(math.degrees(float(s.sigma[1])), 2), round(math.degrees(float(s.sigma[2])), 2)]
        return [round(math.degrees(float(s.sigma[0])), 2)]

    def config(self) -> dict[str, Any]:
        return {
            "sensors": [{"id": s.name, "kind": s.kind, "label": s.label, "type": s.type_id, "pos": s.pos[:2].tolist(),
                         "max_range": s.max_range, "heading_deg": round(math_to_compass(s.heading), 1),
                         "fov_deg": round(math.degrees(s.fov), 1), "pd": s.pd, "sigma": self._sigma_display(s)}
                        for s in list(self.sensors.values())],
            "layout_rev": self.layout_rev,
            "zones": [z.as_dict() for z in self.zones],
            "origin": {"lat": self.origin[0], "lon": self.origin[1]},
            "scan_dt": self.scan_dt,
        }
