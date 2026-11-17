"""HTTP/WebSocket-сервіс поверх Engine. Запуск: python -m uvicorn dronewatch.service.app:app --port 8000

Увесь робочий стан сервісу (рушій, MQTT, WebSocket-клієнти, задача скану) лежить в одному об'єкті Runtime в `app.state.runtime`,
а не в глобальних змінних модуля. Його створює lifespan, обробники отримують через залежність `get_runtime`, а тест підставляє
власний. Тож два застосунки в одному процесі не ділять стан, а «рушій ще не створено» це явна помилка 503, а не `None`."""
import asyncio
import contextlib
import logging
import math
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.requests import HTTPConnection
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from dronewatch.fusion.models import Sensor
from dronewatch.service import config, dashboard_web
from dronewatch.service.ai import agent
from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.ai.llm import MistralClient
from dronewatch.service.ai.rag import IncidentIndex
from dronewatch.service.auth import SensorAuth
from dronewatch.service.dashboard_auth import LoginLimiter, SessionStore, Users
from dronewatch.service.dashboard_web import DashboardAccess, current_user, get_access, require_user
from dronewatch.service.demo import DemoSource, make_sensors
from dronewatch.service.engine import Engine, SensorStatus
from dronewatch.service.mqtt_in import MqttIngest

log = logging.getLogger("dronewatch")
BASE = Path(__file__).resolve().parent
MAX_HTTP_BATCH = 1000          # подій в одному POST /api/events; більше відхиляється (422) ще до обробки
SCAN_ERRORS_BEFORE_RESET = 5   # стільки збоїв скану поспіль, і трекер скидається (стан міг зіпсуватися)


class Detection(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)   # JSON-літерали NaN/Infinity Python приймає, а фільтру вони шкодять: 422
    sensor_id: str
    ts: float | None = None             # обовʼязковий для детекції (рушій відхилить без нього), не потрібен для heartbeat
    azimuth_deg: float | None = None   # обовʼязковий для детекції, не потрібен для heartbeat
    heartbeat: bool = False            # «я живий, але нічого не бачу»
    range_m: float | None = None
    elevation_deg: float | None = None


def default_db_path() -> str:
    return os.environ.get("DB_PATH", str(BASE / "incidents.db"))


@dataclass
class ScanHealth:
    """Стан циклу сканів для /api/health: last_ok це час останнього успішного скану."""
    started: float = field(default_factory=time.time)
    last_ok: float | None = None
    errors: int = 0
    consecutive: int = 0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class AiRuntime:
    """AI-асистент оператора: чат-агент (llm) і RAG-індекс минулих інцидентів (index). Існує лише коли
    ai.enabled і MISTRAL_API_KEY справді задано — інакше Runtime.ai лишається None (build_ai)."""
    llm: MistralClient
    index: IncidentIndex
    max_iterations: int
    limiter: LoginLimiter


@dataclass
class Runtime:
    """Увесь змінюваний стан працюючого сервісу. Лежить в `app.state.runtime`; рушій обовʼязковий, решта має значення за замовчуванням."""
    engine: Engine
    demo: bool = False
    db_path: str = field(default_factory=default_db_path)
    demo_src: DemoSource | None = None
    mqtt: MqttIngest | None = None
    clients: dict[WebSocket, str | None] = field(default_factory=dict)   # вебсокет -> токен сесії (None, коли входу немає)
    scan_task: asyncio.Task[None] | None = None
    scan: ScanHealth = field(default_factory=ScanHealth)
    ai: AiRuntime | None = None


def get_runtime(conn: HTTPConnection) -> Runtime:
    runtime: Runtime | None = getattr(conn.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(503, "Сервіс ще не ініціалізовано")
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def build_engine(cfg: config.Config, sensors: list[Sensor], db_path: str) -> Engine:
    required = cfg.mode == "live" and cfg.auth.mode == "required"
    auth = SensorAuth({s.id: s.key_sha256 for s in cfg.sensors if s.key_sha256}) if required else None
    if cfg.mode == "live" and auth is None:
        log.warning("автентифікацію датчиків вимкнено (auth.mode: disabled): будь-хто з доступом до /api/events чи брокера "
                    "може підкидати події й підробляти heartbeat")
    return Engine(sensors, config.build_zones(cfg), db_path=db_path, scan_dt=cfg.scan_dt,
                  min_age_s=cfg.alerts.min_age_s, require_radar=cfg.alerts.require_radar_in_cover,
                  max_coast=cfg.alerts.max_coast_scans, sensor_timeout_s=cfg.sensor_timeout_s,
                  max_events_per_sensor=cfg.max_events_per_sensor_scan, max_tracks=cfg.max_tracks, auth=auth)


def build_mqtt(cfg: config.Config, engine: Engine) -> MqttIngest | None:
    """Підписка на MQTT, якщо задано брокер (конфіг або MQTT_HOST). Змінні оточення перекривають конфіг (зручно для docker)."""
    m = cfg.mqtt.model_dump() if cfg.mqtt else {}
    host = os.environ.get("MQTT_HOST", m.get("host"))
    if not host:
        return None
    user = os.environ.get("MQTT_USER", m.get("username"))
    ca = os.environ.get("MQTT_CA_CERT", m.get("ca_cert")) or None
    tls = bool(ca) or os.environ.get("MQTT_TLS", str(m.get("tls", False))).strip().lower() in ("1", "true", "yes", "on")
    if user and not tls:
        log.warning("MQTT без TLS: логін, пароль і ключі датчиків ідуть відкритим текстом. Задайте MQTT_CA_CERT "
                    "(або mqtt.ca_cert / mqtt.tls у конфігу), якщо брокер не на довіреному каналі")
    return MqttIngest(engine, host, int(os.environ.get("MQTT_PORT", m.get("port", 1883))), m.get("topic") or "dronewatch/events/#",
                      user, os.environ.get("MQTT_PASSWORD", m.get("password")), tls=tls, ca_cert=ca)


def build_ai(cfg: config.Config) -> AiRuntime | None:
    """AI-асистент — цілком необов'язковий шар. ai.enabled: false чи відсутній MISTRAL_API_KEY: сервіс все одно
    піднімається, лише /api/ai/ask повертає 503 з поясненням замість того, щоб відмовлятись стартувати."""
    if not cfg.ai.enabled:
        return None
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        log.warning("ai.enabled: true, але MISTRAL_API_KEY не задано в оточенні: AI-асистент вимкнено "
                    "(/api/ai/ask відповідатиме 503)")
        return None
    llm = MistralClient(api_key, model=cfg.ai.model, embedding_model=cfg.ai.embedding_model)
    index = IncidentIndex(cfg.ai.qdrant_url, llm.embed)
    return AiRuntime(llm=llm, index=index, max_iterations=cfg.ai.max_tool_iterations,
                     limiter=LoginLimiter(max_failures=10, window_s=60.0))


def build_runtime(cfg: config.Config) -> Runtime:
    """Збирає робочий стан із конфігу. Нічого не запускає: це робить lifespan."""
    demo = cfg.mode == "demo"
    sensors = make_sensors() if demo else config.build_sensors(cfg)   # demo: датчики симулятора, інакше з конфігу
    db_path = default_db_path()
    engine = build_engine(cfg, sensors, db_path)
    if demo:
        engine.calibration = {"done": True, "note": "еталонний політ + компенсація затримок"}
    else:
        engine.calibration = config.calibration_note(cfg)
    return Runtime(engine=engine, demo=demo, db_path=db_path, demo_src=DemoSource(sensors) if demo else None,
                   mqtt=build_mqtt(cfg, engine), ai=build_ai(cfg))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Без цього Python показує лише WARNING і вище, а журнал входів оператора (INFO) не було б видно.
    # Лише при старті сервісу: при імпорті модуля це засмічувало б вивід тестів.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(os.environ.get("CONFIG", BASE / "config.yaml"))
    runtime = build_runtime(cfg)
    if runtime.mqtt:
        runtime.mqtt.start()
    app.state.runtime = runtime
    app.state.access = build_dashboard_access(cfg)
    runtime.scan_task = asyncio.create_task(scan_loop(app, runtime))
    runtime.scan_task.add_done_callback(_scan_task_done)
    yield
    runtime.scan_task.cancel()
    if runtime.mqtt:
        runtime.mqtt.stop()


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
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 без значень, що не пройшли перевірку. Стандартна відповідь їх відлунює (`input`), а NaN/Infinity Starlette
    не вміє серіалізувати в JSON: клієнт отримував 500 замість 422. Заодно не повертаємо відправнику його ж дані."""
    return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}, status_code=422)


def _scan_task_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        log.critical("цикл сканів завершився: %r. Трекінг не працює, /api/health повертає 503", task.exception())


async def broadcast(rt: Runtime, access: DashboardAccess, payload: dict[str, Any]) -> None:
    """Шле стан усім WebSocket-клієнтам; сесії, що скінчилися (або вийшли), закриває замість розсилки."""
    for ws, token in list(rt.clients.items()):
        if not access.session_valid(token):   # сесія скінчилась або користувач вийшов: клієнту більше нічого не шлемо
            rt.clients.pop(ws, None)
            with contextlib.suppress(Exception):
                await ws.close(code=1008)
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            rt.clients.pop(ws, None)


async def scan_step(app: FastAPI, rt: Runtime) -> None:
    """Один скан. Будь-який збій тут не має зупиняти трекінг: ловимо, рахуємо, логуємо й живемо далі."""
    sh = rt.scan
    try:
        truth = None
        if rt.demo_src is not None:
            events, truth, done = rt.demo_src.poll()
            rt.engine.ingest(events)
            if done:
                rt.engine.reset()
                rt.demo_src.restart()
        rt.engine.tick()
        payload = {"type": "state", **rt.engine.state(truth), "alerts": rt.engine.alerts[:30],
                   "mqtt": rt.mqtt.status() if rt.mqtt else None}
        await broadcast(rt, getattr(app.state, "access", None) or DashboardAccess(), payload)
    except Exception as e:  # CancelledError сюди не потрапляє (це BaseException), тож зупинка сервісу працює
        sh.errors += 1
        sh.consecutive += 1
        sh.last_error = f"{type(e).__name__}: {e}"
        rt.engine.count("scan_errors")
        if sh.consecutive == 1 or sh.consecutive % 20 == 0:   # не засмічуємо лог 2 рядками на секунду
            log.exception("збій скану (поспіль: %d, усього: %d)", sh.consecutive, sh.errors)
        if sh.consecutive == SCAN_ERRORS_BEFORE_RESET:
            log.error("%d збоїв поспіль: скидаю трекер (журнал інцидентів лишається)", sh.consecutive)
            try:
                rt.engine.reset()
            except Exception:
                log.exception("скидання трекера теж не вдалося")
    else:
        sh.consecutive = 0
        sh.last_ok = time.time()


async def scan_loop(app: FastAPI, rt: Runtime) -> None:
    while True:
        await scan_step(app, rt)
        await asyncio.sleep(rt.engine.scan_dt)


@app.get("/")
def index(user: Annotated[str | None, Depends(current_user)]) -> Response:
    if user is None:   # сторінку без входу не віддаємо: людину веде на форму входу
        return RedirectResponse("/login", status_code=303)
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/config", dependencies=[Depends(require_user)])
def get_config(rt: RuntimeDep) -> dict[str, Any]:
    return {**rt.engine.config(), "demo": rt.demo}


@app.get("/api/health", response_model=None)   # повертає то dict, то JSONResponse(503): моделі відповіді тут нема
def health(user: Annotated[str | None, Depends(current_user)], rt: RuntimeDep) -> dict[str, Any] | JSONResponse:
    """Для healthcheck і моніторингу. 503, якщо цикл сканів помер або давно не було жодного успішного скану.
    Без входу віддає лише {"ok": ...}: імена втрачених датчиків, адреса брокера й тексти помилок це вже подробиці для оператора."""
    now, sh = time.time(), rt.scan
    stale_after = max(5.0, 10 * rt.engine.scan_dt)
    last = sh.last_ok if sh.last_ok is not None else sh.started
    scanning = rt.scan_task is not None and not rt.scan_task.done() and now - last <= stale_after
    if user is None:
        return {"ok": True} if scanning else JSONResponse({"ok": False}, status_code=503)
    lost = rt.engine.degraded()
    body = {"ok": scanning, "degraded": bool(lost), "lost_sensors": lost, "auth": rt.engine.auth is not None,
            "mode": "demo" if rt.demo else "live", "scans": rt.engine.stats["scans"],
            "scan": {"errors": sh.errors, "consecutive_errors": sh.consecutive, "last_error": sh.last_error,
                     "last_ok_age_s": round(now - sh.last_ok, 1) if sh.last_ok is not None else None},
            "mqtt": rt.mqtt.status() if rt.mqtt else None}
    return body if scanning else JSONResponse(body, status_code=503)


@app.get("/api/sensors", dependencies=[Depends(require_user)])
def sensors(rt: RuntimeDep) -> list[SensorStatus]:
    """Стан здоровʼя кожного датчика: OK / WAITING / LOST."""
    return list(rt.engine.sensor_status().values())


@app.get("/api/state", dependencies=[Depends(require_user)])
def state(rt: RuntimeDep) -> dict[str, Any]:
    return rt.engine.state()


@app.post("/api/events", response_model=None)   # то dict, то JSONResponse(401)
def post_events(events: Annotated[list[Detection], Body(max_length=MAX_HTTP_BATCH)], rt: RuntimeDep,
                x_api_key: Annotated[str | None, Header()] = None) -> dict[str, int] | JSONResponse:
    """Ключ датчика в заголовку X-API-Key (не в тілі: тіла частіше потрапляють у логи). Ключ належить одному датчику:
    події інших датчиків у цьому ж запиті відхиляються. 401, якщо жодної події не прийнято через ключ."""
    r = rt.engine.ingest_detailed([{**e.model_dump(), "key": x_api_key} for e in events])
    if r.unauthorized and not r.accepted:
        return JSONResponse({"detail": "відсутній або хибний ключ датчика (заголовок X-API-Key)"}, status_code=401,
                            headers={"WWW-Authenticate": "ApiKey"})
    return {"accepted": r.accepted, "rejected": r.rejected, "unauthorized": r.unauthorized}


def read_incidents(db_path: str, limit: int = 100) -> list[dict[str, Any]]:
    # окреме з'єднання на запит, і його треба закривати (раніше воно витікало); LIMIT -1 у SQLite = «без ліміту», тож clamp знизу
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/incidents", dependencies=[Depends(require_user)])
def incidents(rt: RuntimeDep, limit: int = 100) -> list[dict[str, Any]]:
    return read_incidents(rt.db_path, limit)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]]


@app.post("/api/ai/ask", dependencies=[Depends(require_user)], response_model=None)
def ask_ai(body: AskRequest, request: Request, rt: RuntimeDep) -> AskResponse | JSONResponse:
    """Чат-агент оператора: читає живий стан і минулі інциденти через тули, нічого не пише в систему.
    503, якщо AI вимкнено чи недоступний — трекінг дронів від цього не залежить."""
    if rt.ai is None:
        return JSONResponse({"detail": "AI-асистент вимкнено або недоступний"}, status_code=503)
    access = get_access(request)
    key = access.token(request) or "anon"
    if wait := rt.ai.limiter.attempt(key):
        raise HTTPException(429, "Забагато запитів до AI-асистента, спробуйте пізніше",
                            headers={"Retry-After": str(math.ceil(wait))})
    try:
        result = agent.ask(body.question, llm=rt.ai.llm, rt=rt, index=rt.ai.index, max_iterations=rt.ai.max_iterations)
    except AiUnavailable:
        return JSONResponse({"detail": "AI-асистент вимкнено або недоступний"}, status_code=503)
    return AskResponse(answer=result.answer, tool_calls=result.tool_calls)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    rt: Runtime | None = getattr(websocket.app.state, "runtime", None)
    access: DashboardAccess = getattr(websocket.app.state, "access", None) or DashboardAccess()
    # до accept: відмова тут стає HTTP 403 на рукостисканні, WebSocket навіть не відкривається. Origin захищає від чужих сайтів
    if rt is None or access.user(websocket) is None or not dashboard_web.origin_ok(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    rt.clients[websocket] = access.token(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        rt.clients.pop(websocket, None)
