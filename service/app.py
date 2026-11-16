"""HTTP/WebSocket-сервіс поверх Engine. Запуск: python -m uvicorn app:app --port 8000"""
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from contextlib import closing
from typing import Annotated, Optional

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from mqtt_in import MqttIngest
from demo import DemoSource, make_sensors
from engine import Engine, Zone

log = logging.getLogger("dronewatch")
BASE = Path(__file__).resolve().parent
MAX_HTTP_BATCH = 1000          # подій в одному POST /api/events; більше відхиляється (422) ще до обробки
SCAN_ERRORS_BEFORE_RESET = 5   # стільки збоїв скану поспіль, і трекер скидається (стан міг зіпсуватися)
DEMO = True  # визначається з config.yaml при старті
DB_PATH = os.environ.get("DB_PATH", str(BASE / "incidents.db"))


class Detection(BaseModel):
    sensor_id: str
    ts: Optional[float] = None             # обовʼязковий для детекції (рушій відхилить без нього), не потрібен для heartbeat
    azimuth_deg: Optional[float] = None   # обовʼязковий для детекції, не потрібен для heartbeat
    heartbeat: bool = False               # «я живий, але нічого не бачу»
    range_m: Optional[float] = None
    elevation_deg: Optional[float] = None


engine: Engine = None
mqtt_ingest: MqttIngest = None
demo_src: DemoSource = None
clients: set = set()
scan_task = None
# стан циклу сканів для /api/health: last_ok = час останнього успішного скану
scan_health = {"started": time.time(), "last_ok": None, "errors": 0, "consecutive": 0, "last_error": None}


@asynccontextmanager
async def lifespan(app):
    global engine, demo_src, DEMO, mqtt_ingest, scan_task
    cfg = config.load(os.environ.get("CONFIG", BASE / "config.yaml"))
    DEMO = cfg.mode == "demo"
    sensors = make_sensors() if DEMO else config.build_sensors(cfg)  # demo: датчики симулятора, інакше з конфігу
    engine = Engine(sensors, config.build_zones(cfg), db_path=DB_PATH, scan_dt=cfg.scan_dt,
                    min_age_s=cfg.alerts.min_age_s, require_radar=cfg.alerts.require_radar_in_cover,
                    max_coast=cfg.alerts.max_coast_scans, sensor_timeout_s=cfg.sensor_timeout_s,
                    max_events_per_sensor=cfg.max_events_per_sensor_scan, max_tracks=cfg.max_tracks)
    if DEMO:
        engine.calibration = {"done": True, "note": "еталонний політ + компенсація затримок"}
        demo_src = DemoSource(sensors)
    else:
        engine.calibration = config.calibration_note(cfg)
    m = cfg.mqtt.model_dump() if cfg.mqtt else {}
    m_host = os.environ.get("MQTT_HOST", m.get("host"))  # змінні оточення перекривають конфіг (зручно для docker)
    if m_host:
        mqtt_ingest = MqttIngest(engine, m_host, int(os.environ.get("MQTT_PORT", m.get("port", 1883))),
                                 m.get("topic") or "dronewatch/events/#",
                                 os.environ.get("MQTT_USER", m.get("username")), os.environ.get("MQTT_PASSWORD", m.get("password")))
        mqtt_ingest.start()
    scan_health.update(started=time.time(), last_ok=None, errors=0, consecutive=0, last_error=None)
    scan_task = asyncio.create_task(scan_loop())
    scan_task.add_done_callback(_scan_task_done)
    yield
    scan_task.cancel()
    if mqtt_ingest:
        mqtt_ingest.stop()


app = FastAPI(title="Drone Watch", lifespan=lifespan)


def _scan_task_done(task):
    if not task.cancelled():
        log.critical("цикл сканів завершився: %r. Трекінг не працює, /api/health повертає 503", task.exception())


async def scan_step():
    """Один скан. Будь-який збій тут не має зупиняти трекінг: ловимо, рахуємо, логуємо й живемо далі."""
    sh = scan_health
    try:
        truth = None
        if demo_src is not None:
            events, truth, done = demo_src.poll()
            engine.ingest(events)
            if done:
                engine.reset()
                demo_src.restart()
        engine.tick()
        payload = {"type": "state", **engine.state(truth), "alerts": engine.alerts[:30],
                   "mqtt": mqtt_ingest.status() if mqtt_ingest else None}
        for ws in list(clients):
            try:
                await ws.send_json(payload)
            except Exception:
                clients.discard(ws)
    except Exception as e:  # CancelledError сюди не потрапляє (це BaseException), тож зупинка сервісу працює
        sh["errors"] += 1
        sh["consecutive"] += 1
        sh["last_error"] = f"{type(e).__name__}: {e}"
        engine.count("scan_errors")
        if sh["consecutive"] == 1 or sh["consecutive"] % 20 == 0:   # не засмічуємо лог 2 рядками на секунду
            log.exception("збій скану (поспіль: %d, усього: %d)", sh["consecutive"], sh["errors"])
        if sh["consecutive"] == SCAN_ERRORS_BEFORE_RESET:
            log.error("%d збоїв поспіль: скидаю трекер (журнал інцидентів лишається)", sh["consecutive"])
            try:
                engine.reset()
            except Exception:
                log.exception("скидання трекера теж не вдалося")
    else:
        sh["consecutive"] = 0
        sh["last_ok"] = time.time()


async def scan_loop():
    while True:
        await scan_step()
        await asyncio.sleep(engine.scan_dt)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/config")
def get_config():
    return {**engine.config(), "demo": DEMO}


@app.get("/api/health")
def health():
    """Для моніторингу та docker healthcheck. 503, якщо цикл сканів помер або давно не було жодного успішного скану."""
    now, sh = time.time(), scan_health
    stale_after = max(5.0, 10 * engine.scan_dt)
    last = sh["last_ok"] if sh["last_ok"] is not None else sh["started"]
    scanning = scan_task is not None and not scan_task.done() and now - last <= stale_after
    lost = engine.degraded()
    body = {"ok": scanning, "degraded": bool(lost), "lost_sensors": lost,
            "mode": "demo" if DEMO else "live", "scans": engine.stats["scans"],
            "scan": {"errors": sh["errors"], "consecutive_errors": sh["consecutive"], "last_error": sh["last_error"],
                     "last_ok_age_s": round(now - sh["last_ok"], 1) if sh["last_ok"] is not None else None},
            "mqtt": mqtt_ingest.status() if mqtt_ingest else None}
    return body if scanning else JSONResponse(body, status_code=503)


@app.get("/api/sensors")
def sensors():
    """Стан здоровʼя кожного датчика: OK / WAITING / LOST."""
    return list(engine.sensor_status().values())


@app.get("/api/state")
def state():
    return engine.state()


@app.post("/api/events")
def post_events(events: Annotated[list[Detection], Body(max_length=MAX_HTTP_BATCH)]):
    ok, bad = engine.ingest([e.model_dump() for e in events])
    return {"accepted": ok, "rejected": bad}


@app.get("/api/incidents")
def incidents(limit: int = 100):
    # окреме з'єднання на запит, і його треба закривати (раніше воно витікало); LIMIT -1 у SQLite = «без ліміту», тож clamp знизу
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(r) for r in rows]


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
