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
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fusion_proto"))
from tracker import Tracker  # noqa: E402

LEVELS = {"NONE": 0, "WARNING": 1, "CRITICAL": 2}
STALE_S = 2.0          # події старші за це відкидаються; події з майбутнього (розбіжність годинників) теж, понад цей допуск
RANGE_SLACK = 1.5      # радар може трохи перебрати max_range (шум, повільне згасання), але не вдвічі
OK, WAITING, LOST = "OK", "WAITING", "LOST"   # стан здоровʼя датчика


def compass_to_math(deg):
    return math.radians(90.0 - deg)


def math_to_compass(rad):
    return (90.0 - math.degrees(rad)) % 360.0


class Zone:
    def __init__(self, name, radius, level, center=(0.0, 0.0)):
        self.name, self.radius, self.level, self.center = name, radius, level, np.asarray(center, float)

    def contains(self, pos):
        return np.linalg.norm(pos[:2] - self.center) <= self.radius

    def as_dict(self):
        return {"name": self.name, "radius": self.radius, "level": self.level, "center": self.center.tolist()}


class Engine:
    def __init__(self, sensors, zones, db_path=":memory:", scan_dt=0.5, min_age_s=5.0, require_radar=True, max_coast=2,
                 sensor_timeout_s=5.0, clock=time.time, max_events_per_sensor=150, max_tracks=200):
        # min_age_s: тривога лише для треку, підтвердженого щонайменше стільки секунд (відсів короткоживучих хибних)
        # require_radar: у зоні радара тривога лише з радарним підтвердженням; max_coast: скільки сканів без детекцій трек ще «живий»
        self.min_age_s, self.require_radar, self.max_coast = min_age_s, require_radar, max_coast
        self.sensor_timeout_s, self.clock = sensor_timeout_s, clock   # clock підміняється в тестах
        # Стелі проти потопу подій: понад них події відкидаються й рахуються, але скан лишається в межах бюджету часу.
        self.max_events_per_sensor, self.max_tracks = max_events_per_sensor, max_tracks
        self.sensors = {s.name: s for s in sensors}
        self.zones = zones
        self.scan_dt = scan_dt
        self.tracker = Tracker(sensors, max_tracks=max_tracks)
        self.t0 = self.started = self.clock()
        self.last_seen = {}      # sensor_id -> час, коли від датчика востаннє щось прийшло (подія або heartbeat)
        self.heard = set()       # датчики, що озивалися з попереднього скану
        self.buffer = []
        self.buf_count = Counter()   # подій у буфері від кожного датчика (для стелі)
        # Один замок на весь змінюваний стан: його беруть і HTTP-потоки (ingest, state), і потік MQTT, і цикл сканів.
        self.lock = threading.Lock()
        self.max_level = {}      # track_id -> найвищий рівень, що вже дав тривогу
        self.alerts = []         # в памʼяті останні; повний журнал у SQLite
        self.stats = {"events_in": 0, "events_rejected": 0, "events_dropped": 0, "heartbeats": 0, "scans": 0,
                      "scan_errors": 0, "tracks_capped": 0, "bearings_truncated": 0}
        self.calibration = {"done": False, "note": "не виконано"}
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, track_id INTEGER, level TEXT, zone TEXT,
            x REAL, y REAL, z REAL, speed REAL, sensors TEXT)""")
        self.db.commit()

    # --- вхід
    def reset(self):
        """Новий сеанс трекінгу (журнал інцидентів лишається)."""
        with self.lock:
            self.tracker = Tracker(list(self.sensors.values()), max_tracks=self.max_tracks)
            self.buffer.clear()
            self.buf_count.clear()
            self.max_level.clear()
            self.t0 = self.clock()

    def count(self, key, n=1):
        """Потокобезпечний лічильник: `stats[k] += n` із кількох потоків (HTTP, MQTT) губить приріст."""
        with self.lock:
            self.stats[key] += n

    def ingest(self, events):
        """Валідує події й кладе у буфер до наступного скану. Повертає (прийнято, відхилено).
        Відхилені = невалідні + відкинуті стелею (окремо вони в stats: events_rejected / events_dropped).
        Валідація йде поза замком, у буфер і лічильники пакет потрапляє одним захопленням замка."""
        now = self.clock()
        beats, parsed, bad, dropped = [], [], 0, 0
        taken = Counter()   # підказка без замка: не валідуємо більше за стелю від одного датчика в пакеті
        for e in events:
            sid = e.get("sensor_id")
            if not isinstance(sid, str):   # з MQTT може прийти що завгодно, зокрема нехешований список: це не повинно валити ingest
                bad += 1
                continue
            if e.get("heartbeat"):
                if sid in self.sensors:
                    beats.append(sid)
                else:
                    bad += 1
                continue
            if taken[sid] >= self.max_events_per_sensor:
                dropped += 1
                continue
            z = self._to_internal(e, now)
            if z is None:
                bad += 1
            else:
                taken[sid] += 1
                parsed.append(z)
        ok = 0
        with self.lock:
            for sid in beats:
                self._mark(sid, now)
            for z in parsed:
                if self.buf_count[z[0]] >= self.max_events_per_sensor:   # остаточна перевірка: буфер спільний між потоками
                    dropped += 1
                    continue
                self.buf_count[z[0]] += 1
                self.buffer.append(z)
                self._mark(z[0], now)
                ok += 1
            self.stats["events_in"] += ok
            self.stats["heartbeats"] += len(beats)
            self.stats["events_rejected"] += bad
            self.stats["events_dropped"] += dropped
        return ok + len(beats), bad + dropped

    def _mark(self, sensor_id, now):
        """Датчик озвався. Викликати лише під self.lock."""
        self.last_seen[sensor_id] = now
        self.heard.add(sensor_id)

    def _to_internal(self, e, now):
        """Подія -> (датчик, вимірювання) або None. Вхід недовірений (HTTP, MQTT): NaN/Infinity у JSON, нефізичні значення
        й мітки часу з майбутнього відхиляються тут, а не доходять до фільтра (один NaN робив трек з NaN у стані)."""
        s = self.sensors.get(e.get("sensor_id"))
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
        z[1 if s.kind == "radar" else 0] = (z[1 if s.kind == "radar" else 0] + math.pi) % (2 * math.pi) - math.pi
        return (s.name, z)

    # --- скан
    def tick(self):
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

    def _evaluate(self, t):
        new_alerts = []
        live_ids = set()
        for tr in self.tracker.confirmed():
            live_ids.add(tr.id)
            if tr.misses > self.max_coast or t - tr.confirmed_at < self.min_age_s:
                continue
            if self.require_radar and self.tracker._in_radar_cover(tr) and t - tr.kinds["radar"] > 3.0:
                continue  # у зоні радара тривогу без радарного підтвердження не піднімаємо
            level, zone = "NONE", None
            for z in self.zones:
                if z.contains(tr.x[:3]) and LEVELS[z.level] > LEVELS[level]:
                    level, zone = z.level, z
            if LEVELS[level] > LEVELS[self.max_level.get(tr.id, "NONE")]:
                self.max_level[tr.id] = level
                a = {"ts": self.clock(), "track_id": tr.id, "level": level, "zone": zone.name,
                     "pos": [round(float(v), 1) for v in tr.x[:3]],
                     "speed": round(float(np.linalg.norm(tr.x[3:5])), 1),
                     "sensors": sorted(k for k, ts in tr.kinds.items() if t - ts < 3.0)}
                new_alerts.append(a)
        for a in new_alerts:
            self.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (?,?,?,?,?,?,?,?,?)",
                            (a["ts"], a["track_id"], a["level"], a["zone"], *a["pos"], a["speed"], ",".join(a["sensors"])))
        if new_alerts:
            self.db.commit()
            self.alerts = (new_alerts[::-1] + self.alerts)[:200]
        for tid in list(self.max_level):
            if tid not in {tr.id for tr in self.tracker.tracks}:
                del self.max_level[tid]
        return new_alerts

    # --- вихід
    def sensor_status(self):
        with self.lock:
            return self._sensor_status()

    def _sensor_status(self):
        """Стан кожного датчика (під self.lock): OK (озивався щойно), WAITING (ще не озивався, але час очікування не вийшов),
        LOST (мовчить довше за sensor_timeout_s). Відмову видно за мовчанням, а не за самими детекціями."""
        now = self.clock()
        out = {}
        for sid, s in self.sensors.items():
            seen = self.last_seen.get(sid)
            age = now - (seen if seen is not None else self.started)
            if seen is not None and age <= self.sensor_timeout_s:
                st = OK
            elif seen is None and age <= self.sensor_timeout_s:
                st = WAITING
            else:
                st = LOST
            out[sid] = {"id": sid, "label": s.label, "state": st,
                        "age_s": round(age, 1) if seen is not None else None}
        return out

    def degraded(self):
        return [sid for sid, st in self.sensor_status().items() if st["state"] == LOST]

    def state(self, truth=None):
        # треки змінюються в tick() з іншого потоку: без замка /api/state міг би прочитати напіноновлений стан
        with self.lock:
            return self._state(truth)

    def _state(self, truth):
        t = self.clock() - self.t0
        tracks = []
        for tr in self.tracker.confirmed():
            level = "NONE"
            for z in self.zones:
                if z.contains(tr.x[:3]) and LEVELS[z.level] > LEVELS[level]:
                    level = z.level
            tracks.append({
                "id": tr.id, "x": round(float(tr.x[0]), 1), "y": round(float(tr.x[1]), 1), "z": round(float(tr.x[2]), 1),
                "vx": round(float(tr.x[3]), 1), "vy": round(float(tr.x[4]), 1),
                "speed": round(float(np.linalg.norm(tr.x[3:5])), 1),
                "err": round(float(math.sqrt(max(tr.P[0, 0] + tr.P[1, 1], 0))), 1),
                "score": round(float(tr.score(t)), 2),
                "kinds": sorted(k for k, ts in tr.kinds.items() if t - ts < 3.0),
                "coasting": tr.misses > self.max_coast, "level": level,
            })
        out = {"t": round(t, 1), "tracks": tracks, "stats": dict(self.stats), "calibration": self.calibration,
               "sensors": list(self._sensor_status().values()),
               "tentative": sum(1 for tr in self.tracker.tracks if tr.state == "TENTATIVE")}
        if truth is not None:
            out["truth"] = {k: [round(float(v), 1) for v in p[:3]] for k, p in truth.items()}
        return out

    def config(self):
        return {
            "sensors": [{"id": s.name, "kind": s.kind, "label": s.label, "pos": s.pos[:2].tolist(), "max_range": s.max_range}
                        for s in self.sensors.values()],
            "zones": [z.as_dict() for z in self.zones],
            "scan_dt": self.scan_dt,
        }
