"""HTTP/WebSocket-сервіс поверх Engine. Запуск: python -m uvicorn app:app --port 8000"""
import asyncio
import contextlib
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

import config
import dashboard_web
from auth import SensorAuth
from dashboard_auth import LoginLimiter, SessionStore, Users
from dashboard_web import DashboardAccess, current_user, require_user
from demo import DemoSource, make_sensors
from engine import Engine
from mqtt_in import MqttIngest

log = logging.getLogger("dronewatch")
BASE = Path(__file__).resolve().parent
MAX_HTTP_BATCH = 1000          # подій в одному POST /api/events; більше відхиляється (422) ще до обробки
SCAN_ERRORS_BEFORE_RESET = 5   # стільки збоїв скану поспіль, і трекер скидається (стан міг зіпсуватися)
DEMO = True  # визначається з config.yaml при старті
DB_PATH = os.environ.get("DB_PATH", str(BASE / "incidents.db"))


class Detection(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)   # JSON-літерали NaN/Infinity Python приймає, а фільтру вони шкодять: 422
    sensor_id: str
    ts: float | None = None             # обовʼязковий для детекції (рушій відхилить без нього), не потрібен для heartbeat
    azimuth_deg: float | None = None   # обовʼязковий для детекції, не потрібен для heartbeat
    heartbeat: bool = False            # «я живий, але нічого не бачу»
    range_m: float | None = None
    elevation_deg: float | None = None


engine: Engine | None = None
mqtt_ingest: MqttIngest | None = None
demo_src: DemoSource | None = None
clients: dict[WebSocket, str | None] = {}   # вебсокет -> токен сесії (None, коли автентифікацію вимкнено)
scan_task = None
# стан циклу сканів для /api/health: last_ok = час останнього успішного скану
scan_health = {"started": time.time(), "last_ok": None, "errors": 0, "consecutive": 0, "last_error": None}


@asynccontextmanager
async def lifespan(app):
    global engine, demo_src, DEMO, mqtt_ingest, scan_task
    # Без цього Python показує лише WARNING і вище, а журнал входів оператора (INFO) не було б видно. Тільки при старті сервісу:
    # при імпорті модуля це засмічувало б вивід тестів.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(os.environ.get("CONFIG", BASE / "config.yaml"))
    DEMO = cfg.mode == "demo"
    sensors = make_sensors() if DEMO else config.build_sensors(cfg)  # demo: датчики симулятора, інакше з конфігу
    auth = SensorAuth({s.id: s.key_sha256 for s in cfg.sensors}) if not DEMO and cfg.auth.mode == "required" else None
    if not DEMO and auth is None:
        log.warning("автентифікацію датчиків вимкнено (auth.mode: disabled): будь-хто з доступом до /api/events чи брокера "
                    "може підкидати події й підробляти heartbeat")
    engine = Engine(sensors, config.build_zones(cfg), db_path=DB_PATH, scan_dt=cfg.scan_dt,
                    min_age_s=cfg.alerts.min_age_s, require_radar=cfg.alerts.require_radar_in_cover,
                    max_coast=cfg.alerts.max_coast_scans, sensor_timeout_s=cfg.sensor_timeout_s,
                    max_events_per_sensor=cfg.max_events_per_sensor_scan, max_tracks=cfg.max_tracks, auth=auth)
    if DEMO:
        engine.calibration = {"done": True, "note": "еталонний політ + компенсація затримок"}
        demo_src = DemoSource(sensors)
    else:
        engine.calibration = config.calibration_note(cfg)
    m = cfg.mqtt.model_dump() if cfg.mqtt else {}
    m_host = os.environ.get("MQTT_HOST", m.get("host"))  # змінні оточення перекривають конфіг (зручно для docker)
    if m_host:
        m_user = os.environ.get("MQTT_USER", m.get("username"))
        m_ca = os.environ.get("MQTT_CA_CERT", m.get("ca_cert")) or None
        m_tls = bool(m_ca) or os.environ.get("MQTT_TLS", str(m.get("tls", False))).strip().lower() in ("1", "true", "yes", "on")
        if m_user and not m_tls:
            log.warning("MQTT без TLS: логін, пароль і ключі датчиків ідуть відкритим текстом. Задайте MQTT_CA_CERT "
                        "(або mqtt.ca_cert / mqtt.tls у конфігу), якщо брокер не на довіреному каналі")
        mqtt_ingest = MqttIngest(engine, m_host, int(os.environ.get("MQTT_PORT", m.get("port", 1883))),
                                 m.get("topic") or "dronewatch/events/#", m_user,
                                 os.environ.get("MQTT_PASSWORD", m.get("password")), tls=m_tls, ca_cert=m_ca)
        mqtt_ingest.start()
    app.state.access = build_dashboard_access(cfg)
    scan_health.update(started=time.time(), last_ok=None, errors=0, consecutive=0, last_error=None)
    scan_task = asyncio.create_task(scan_loop())
    scan_task.add_done_callback(_scan_task_done)
    yield
    scan_task.cancel()
    if mqtt_ingest:
        mqtt_ingest.stop()


def build_dashboard_access(cfg: config.Config) -> DashboardAccess:
    """У demo входу немає (локальна розробка). У live за замовчуванням обов'язковий; вимкнути можна лише свідомо."""
    if cfg.mode == "demo":
        return DashboardAccess()
    if cfg.dashboard.auth == "disabled":
        log.warning("вхід у дашборд вимкнено (dashboard.auth: disabled): стан, тривоги й WebSocket відкриті всім, хто дістанеться порту")
        return DashboardAccess()
    return DashboardAccess(Users({u.name: u.password_hash for u in cfg.dashboard.users}),
                           SessionStore(cfg.dashboard.session_ttl_s), LoginLimiter())


app = FastAPI(title="Drone Watch", lifespan=lifespan)
app.include_router(dashboard_web.router)
app.middleware("http")(dashboard_web.add_security_headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """422 без значень, що не пройшли перевірку. Стандартна відповідь їх відлунює (`input`), а NaN/Infinity Starlette
    не вміє серіалізувати в JSON: клієнт отримував 500 замість 422. Заодно не повертаємо відправнику його ж дані."""
    return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}, status_code=422)


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
        access: DashboardAccess = getattr(app.state, "access", None) or DashboardAccess()
        for ws, token in list(clients.items()):
            if not access.session_valid(token):   # сесія скінчилась або користувач вийшов: клієнту більше нічого не шлемо
                clients.pop(ws, None)
                with contextlib.suppress(Exception):
                    await ws.close(code=1008)
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                clients.pop(ws, None)
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
def index(user: Annotated[str | None, Depends(current_user)]):
    if user is None:   # сторінку без входу не віддаємо: людину веде на форму входу
        return RedirectResponse("/login", status_code=303)
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/config", dependencies=[Depends(require_user)])
def get_config():
    return {**engine.config(), "demo": DEMO}


@app.get("/api/health")
def health(user: Annotated[str | None, Depends(current_user)]):
    """Для healthcheck і моніторингу. 503, якщо цикл сканів помер або давно не було жодного успішного скану.
    Без входу віддає лише {"ok": ...}: імена втрачених датчиків, адреса брокера й тексти помилок це вже подробиці для оператора."""
    now, sh = time.time(), scan_health
    stale_after = max(5.0, 10 * engine.scan_dt)
    last = sh["last_ok"] if sh["last_ok"] is not None else sh["started"]
    scanning = scan_task is not None and not scan_task.done() and now - last <= stale_after
    if user is None:
        return {"ok": True} if scanning else JSONResponse({"ok": False}, status_code=503)
    lost = engine.degraded()
    body = {"ok": scanning, "degraded": bool(lost), "lost_sensors": lost, "auth": engine.auth is not None,
            "mode": "demo" if DEMO else "live", "scans": engine.stats["scans"],
            "scan": {"errors": sh["errors"], "consecutive_errors": sh["consecutive"], "last_error": sh["last_error"],
                     "last_ok_age_s": round(now - sh["last_ok"], 1) if sh["last_ok"] is not None else None},
            "mqtt": mqtt_ingest.status() if mqtt_ingest else None}
    return body if scanning else JSONResponse(body, status_code=503)


@app.get("/api/sensors", dependencies=[Depends(require_user)])
def sensors():
    """Стан здоровʼя кожного датчика: OK / WAITING / LOST."""
    return list(engine.sensor_status().values())


@app.get("/api/state", dependencies=[Depends(require_user)])
def state():
    return engine.state()


@app.post("/api/events")
def post_events(events: Annotated[list[Detection], Body(max_length=MAX_HTTP_BATCH)],
                x_api_key: Annotated[str | None, Header()] = None):
    """Ключ датчика в заголовку X-API-Key (не в тілі: тіла частіше потрапляють у логи). Ключ належить одному датчику:
    події інших датчиків у цьому ж запиті відхиляються. 401, якщо жодної події не прийнято через ключ."""
    r = engine.ingest_detailed([{**e.model_dump(), "key": x_api_key} for e in events])
    if r.unauthorized and not r.accepted:
        return JSONResponse({"detail": "відсутній або хибний ключ датчика (заголовок X-API-Key)"}, status_code=401,
                            headers={"WWW-Authenticate": "ApiKey"})
    return {"accepted": r.accepted, "rejected": r.rejected, "unauthorized": r.unauthorized}


@app.get("/api/incidents", dependencies=[Depends(require_user)])
def incidents(limit: int = 100):
    # окреме з'єднання на запит, і його треба закривати (раніше воно витікало); LIMIT -1 у SQLite = «без ліміту», тож clamp знизу
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(r) for r in rows]


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    access: DashboardAccess = getattr(app.state, "access", None) or DashboardAccess()
    # до accept: відмова тут стає HTTP 403 на рукостисканні, WebSocket навіть не відкривається. Origin захищає від чужих сайтів
    if access.user(websocket) is None or not dashboard_web.origin_ok(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    clients[websocket] = access.token(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.pop(websocket, None)
