"""HTTP/WebSocket-сервіс поверх Engine. Запуск: python -m uvicorn dronewatch.service.app:app --port 8000

Увесь робочий стан сервісу (рушій, MQTT, WebSocket-клієнти, задача скану) лежить в одному об'єкті Runtime в `app.state.runtime`,
а не в глобальних змінних модуля. Його створює lifespan, обробники отримують через залежність `get_runtime`, а тест підставляє
власний. Тож два застосунки в одному процесі не ділять стан, а «рушій ще не створено» це явна помилка 503, а не `None`."""
import asyncio
import contextlib
import logging
import math
import os
import re
import sqlite3
import time
import uuid
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.requests import HTTPConnection
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from dronewatch.fusion.models import Sensor
from dronewatch.service import config, coverage, dashboard_redis, dashboard_web, layout
from dronewatch.service.ai import agent
from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.ai.llm import MistralClient
from dronewatch.service.ai.rag import IncidentIndex
from dronewatch.service.audit import AuditEntry, AuditError, VerifyResult
from dronewatch.service.auth import SensorAuth
from dronewatch.service.dashboard_auth import (
    LoginLimiter,
    SessionStore,
    SessionStoreError,
    UserEntry,
    Users,
    check_redis_url,
    has_role,
)
from dronewatch.service.dashboard_web import (
    AUDIT_UNAVAILABLE,
    DashboardAccess,
    Identity,
    audit_best_effort,
    audit_or_503,
    client_ip,
    current_user,
    json_post_guard,
    page_identity,
    require_role,
    same_origin_guard,
)
from dronewatch.service.demo import DemoSource, make_sensors
from dronewatch.service.engine import Alert, Engine, SensorStatus, Zone
from dronewatch.service.mqtt_in import MqttIngest
from dronewatch.service.occlusion import Buildings, LineOfSight
from dronewatch.service.replay import ReplayOptions, read_replay
from dronewatch.service.terrain import Terrain

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
    drone_model: str | None = Field(default=None, max_length=40)   # ключ моделі з RF-детектора (див. catalog.py)


def default_db_path() -> str:
    """DB_PATH, інакше ./data/incidents.db відносно робочої теки (не всередині встановленого пакета)."""
    env = os.environ.get("DB_PATH")
    if env:
        return env
    path = Path.cwd() / "data" / "incidents.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


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
    ai_tasks: set[asyncio.Task[None]] = field(default_factory=set)   # живі посилання на фонові задачі AI-індексації
    map_file: Path | None = None   # власний PMTiles карти, якщо файл є на диску
    map_online: bool = False       # дозвіл на онлайн-тайли (лише коли власного файлу немає)
    los: LineOfSight | None = None   # рельєф і будинки для лінії видимості в покритті (None: файлів немає чи вони зіпсовані)
    target_alt: float = coverage.TARGET_ALT_M
    audit_status: VerifyResult | None = None   # результат перевірки ланцюжка журналу дій при старті (None: не перевірялось)


def get_runtime(conn: HTTPConnection) -> Runtime:
    runtime: Runtime | None = getattr(conn.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(503, "Сервіс ще не ініціалізовано")
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
# Ролі маршрутів (viewer < operator < admin). Кожен маршрут, крім allowlist у tests/test_roles.py, мусить мати require_role
OperatorDep = Annotated[Identity, Depends(require_role("operator"))]
AdminDep = Annotated[Identity, Depends(require_role("admin"))]


def build_engine(cfg: config.Config, sensors: list[Sensor], db_path: str, zones: list[Zone] | None = None) -> Engine:
    required = cfg.mode == "live" and cfg.auth.mode == "required"
    auth = SensorAuth({s.id: s.key_sha256 for s in cfg.sensors if s.key_sha256}) if required else None
    if cfg.mode == "live" and auth is None:
        log.warning("автентифікацію датчиків вимкнено (auth.mode: disabled): будь-хто з доступом до /api/events чи брокера "
                    "може підкидати події й підробляти heartbeat")
    return Engine(sensors, zones if zones is not None else config.build_zones(cfg), db_path=db_path, scan_dt=cfg.scan_dt,
                  min_age_s=cfg.alerts.min_age_s, require_radar=cfg.alerts.require_radar_in_cover,
                  max_coast=cfg.alerts.max_coast_scans, sensor_timeout_s=cfg.sensor_timeout_s,
                  max_events_per_sensor=cfg.max_events_per_sensor_scan, max_tracks=cfg.max_tracks, auth=auth,
                  origin=(cfg.origin.lat, cfg.origin.lon),
                  replay=ReplayOptions(cfg.replay.before_s, cfg.replay.after_s, cfg.replay.step_s, cfg.replay.retention_days)
                  if cfg.replay.enabled else None)


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


def load_line_of_sight(cfg: config.Config) -> LineOfSight | None:
    """Рельєф і будинки для лінії видимості. Немає файлу: так і працюємо, покриття каже, чого не враховано. Зіпсований файл: те саме,
    але ERROR у лозі, бо оператор вважатиме, що це враховано."""
    origin = (cfg.origin.lat, cfg.origin.lon)
    parts: dict[str, Any] = {}
    for key, loader, path in (("buildings", Buildings.load, Path(cfg.map.buildings)), ("terrain", Terrain.load, Path(cfg.map.dem))):
        if not path.is_file():
            continue
        try:
            parts[key] = loader(path, origin)
        except Exception:   # будь-яка помилка читання: сервіс не падає, але й не вдає, що це враховано
            log.exception("лінія видимості (%s): не вдалося прочитати %s, покриття буде без цього", key, path)
    if not parts:
        return None
    log.info("лінія видимості: %s", ", ".join(parts))
    return LineOfSight(**parts)


def build_runtime(cfg: config.Config) -> Runtime:
    """Збирає робочий стан із конфігу. Нічого не запускає: це робить lifespan."""
    demo = cfg.mode == "demo"
    sensors = make_sensors() if demo else config.build_sensors(cfg)   # demo: датчики симулятора, інакше з конфігу
    db_path = default_db_path()
    map_file = Path(cfg.map.pmtiles)
    engine = build_engine(cfg, sensors, db_path, layout.derive_zones(sensors) if demo else None)   # demo: зони з радарів, а не з конфігу
    if demo:
        engine.calibration = {"done": True, "note": "еталонний політ + компенсація затримок"}
    else:
        engine.calibration = config.calibration_note(cfg)
    return Runtime(engine=engine, demo=demo, db_path=db_path, demo_src=DemoSource(sensors) if demo else None,
                   mqtt=build_mqtt(cfg, engine), ai=build_ai(cfg),
                   map_file=map_file if map_file.is_file() else None, map_online=cfg.map.online_tiles,
                   los=load_line_of_sight(cfg), target_alt=cfg.map.target_alt_m)


def check_audit_chain(rt: Runtime) -> VerifyResult | None:
    """Одноразова перевірка ланцюжка при старті (до ingest, тож замок рушія нікого не стопорить). Пошкоджений журнал не
    зупиняє сервіс: захист від дронів важливіший, але ERROR у лозі й audit.ok=false у health це видно."""
    try:
        res = rt.engine.audit.verify()
    except Exception:   # будь-який збій читання журналу не має валити старт
        log.exception("журнал дій: не вдалося перевірити ланцюжок")
        return None
    if res.ok:
        log.info("журнал дій: %d записів, head=%s", res.rows, res.head)
    else:
        log.error("журнал дій ПОШКОДЖЕНО з запису id=%d", res.broken_at)
    return res


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Без цього Python показує лише WARNING і вище, а журнал входів оператора (INFO) не було б видно.
    # Лише при старті сервісу: при імпорті модуля це засмічувало б вивід тестів.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(os.environ.get("CONFIG", BASE / "config.yaml"))
    runtime = build_runtime(cfg)
    runtime.audit_status = check_audit_chain(runtime)
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
    """Вхід обов'язковий у live (за замовчуванням) і в demo, якщо в конфігу є користувачі. Без входу (demo без користувачів
    або свідоме `dashboard.auth: disabled`) анонім отримує лише viewer: читає, але нічого не змінює."""
    if cfg.dashboard.auth == "disabled":
        log.warning("вхід у дашборд вимкнено (dashboard.auth: disabled): стан, тривоги й WebSocket відкриті для перегляду всім, "
                    "хто дістанеться порту; рішення по тривогах, AI, розкладка й журнал недоступні без входу")
        return DashboardAccess()
    if cfg.mode == "demo" and not cfg.dashboard.users:
        log.warning("демо без користувачів дашборда: лише перегляд (viewer). Щоб редагувати розкладку, додайте в dashboard.users "
                    "користувача з role: admin (dronewatch-keygen --dashboard-user ІМ'Я --role admin)")
        return DashboardAccess()
    users = Users({u.name: UserEntry(u.password_hash, u.role) for u in cfg.dashboard.users})
    if cfg.dashboard.session_store == "redis":
        url = os.environ.get("DRONEWATCH_REDIS_URL") or cfg.dashboard.redis_url   # пароль краще в середовищі, не в конфігу
        if not url:
            raise ValueError("dashboard.session_store: redis потребує dashboard.redis_url або DRONEWATCH_REDIS_URL")
        check_redis_url(url)
        client = dashboard_redis.connect(url)   # недоступний Redis: сервіс не стартує (SessionStoreError)
        log.info("сесії та ліміт входів спільні через Redis")
        return DashboardAccess(users, dashboard_redis.RedisSessionStore(client, cfg.dashboard.session_ttl_s),
                               dashboard_redis.RedisLoginLimiter(client))
    return DashboardAccess(users, SessionStore(cfg.dashboard.session_ttl_s), LoginLimiter())


app = FastAPI(title="Drone Watch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)   # схема API не публічна
app.include_router(dashboard_web.router)
app.middleware("http")(dashboard_web.add_security_headers)
app.add_middleware(dashboard_web.BodyLimitMiddleware)   # зовнішній шар: завелике тіло відсікається до будь-якого розбору


@app.exception_handler(SessionStoreError)
async def session_store_error(request: Request, exc: SessionStoreError) -> JSONResponse:
    """Спільне сховище сесій недоступне: 503 (fail-closed), без подробиць."""
    return JSONResponse({"detail": "Сховище сесій недоступне"}, status_code=503, headers={"Retry-After": "5"})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 без значень, що не пройшли перевірку. Стандартна відповідь їх відлунює (`input`), а NaN/Infinity Starlette
    не вміє серіалізувати в JSON: клієнт отримував 500 замість 422. Заодно не повертаємо відправнику його ж дані."""
    return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}, status_code=422)


def _scan_task_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        log.critical("цикл сканів завершився: %r. Трекінг не працює, /api/health повертає 503", task.exception())


async def _index_alert_in_background(index: IncidentIndex, alert: Alert) -> None:
    """Qdrant/ембединги — мережевий виклик; asyncio.to_thread не дає йому заблокувати event loop (WebSocket, HTTP)
    на час запиту. Ловимо геть усе (не лише AiUnavailable): непередбачена помилка самого клієнта Qdrant не має
    права дійти до scan_step і порахуватись помилкою скану чи спричинити скидання трекера."""
    row = {**alert, "sensors": ",".join(alert["sensors"])}
    try:
        await asyncio.to_thread(index.index, str(uuid.uuid4()), row)
    except Exception as e:
        log.warning("AI: не вдалося проіндексувати інцидент у Qdrant: %s", e)


def _schedule_ai_indexing(rt: Runtime, alert: Alert) -> None:
    assert rt.ai is not None
    task = asyncio.create_task(_index_alert_in_background(rt.ai.index, alert))
    rt.ai_tasks.add(task)
    task.add_done_callback(rt.ai_tasks.discard)


SEND_TIMEOUT_S = 1.0   # клієнт, що не прийняв кадр за цей час, відстає надто: відключаємо, а не гальмуємо скани решти


async def _send(rt: Runtime, ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(ws.send_json(payload), SEND_TIMEOUT_S)
    except Exception:   # обрив, помилка або таймаут (TimeoutError): прибираємо клієнта з розсилки
        rt.clients.pop(ws, None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1013), SEND_TIMEOUT_S)


async def broadcast(rt: Runtime, access: DashboardAccess, payload: dict[str, Any]) -> None:
    """Шле стан усім WebSocket-клієнтам паралельно й з таймаутом: повільний клієнт не гальмує інших і скани.
    Сесії, що скінчилися (або вийшли), закриває замість розсилки."""
    sends = []
    clients = list(rt.clients.items())
    try:   # у потоці: спільне сховище сесій (Redis) блокує, а цикл подій не має чекати
        valid = await asyncio.to_thread(lambda: [access.session_valid(token) for _, token in clients])
    except SessionStoreError:
        log.error("сховище сесій недоступне: розсилку пропущено (fail-closed)")
        return
    for (ws, _token), ok in zip(clients, valid, strict=True):
        if not ok:   # сесія скінчилась або користувач вийшов: клієнту більше нічого не шлемо
            rt.clients.pop(ws, None)
            with contextlib.suppress(Exception):
                await ws.close(code=1008)
            continue
        sends.append(_send(rt, ws, payload))
    if sends:
        await asyncio.gather(*sends)


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
        new_alerts = rt.engine.tick()
        payload = {"type": "state", **rt.engine.state(truth), "alerts": rt.engine.alerts[:30],
                   "mqtt": rt.mqtt.status() if rt.mqtt else None}
        await broadcast(rt, getattr(app.state, "access", None) or DashboardAccess(), payload)
        if rt.ai is not None:
            for alert in new_alerts:
                _schedule_ai_indexing(rt, alert)
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
def index(ident: Annotated[Identity | None, Depends(page_identity)]) -> Response:
    if ident is None or not has_role(ident.role, "viewer"):   # без входу (чи з невідомою роллю) сторінку не віддаємо: на форму входу
        return RedirectResponse("/login", status_code=303)
    return FileResponse(BASE / "static" / "index.html")


PANELS = ("alerts", "tracks")   # панелі, які можна відкріпити в окреме вікно (на інший монітор)


@app.get("/panel/{name}", include_in_schema=False)
def panel(name: str, ident: Annotated[Identity | None, Depends(page_identity)]) -> Response:
    """Та сама сторінка, але JS показує лише одну панель (тривоги чи треки). Вхід і роль ті самі, що для `/`: окреме вікно
    не дає нічого, чого немає в основному. Невідому панель віддаємо як 404 (жодних шляхів із клієнта)."""
    if ident is None or not has_role(ident.role, "viewer"):
        return RedirectResponse("/login", status_code=303)
    if name not in PANELS:
        return Response(status_code=404)
    return FileResponse(BASE / "static" / "index.html")


VENDOR_FILES = {"maplibre-gl.js": "text/javascript", "maplibre-gl.css": "text/css", "three.module.min.js": "text/javascript",
                "pmtiles.js": "text/javascript"}


@app.get("/static/vendor/{name}", include_in_schema=False)
def vendor(name: str) -> Response:   # бібліотеки віддаємо самі, а не з CDN: без стороннього коду на сторінці
    if name not in VENDOR_FILES:   # лише відомі файли: жодних шляхів із клієнта
        return Response(status_code=404)
    return FileResponse(BASE / "static" / "vendor" / name, media_type=VENDOR_FILES[name])


# Код і стилі самого дашборда. JS це ES-модулі: index.html підключає лише main.js, решту він тягне через import (сервер віддає
# тільки файли з цього списку, тож новий модуль треба додати сюди).
APP_FILES = {"dashboard.css": "text/css", **{f"{n}.js": "text/javascript" for n in
             ("core", "map", "viewer3d", "dossier", "panels", "sound", "editor", "audit", "replay", "layout", "main")}}


@app.get("/static/app/{name}", include_in_schema=False)
def app_file(name: str) -> Response:   # як і vendor: лише відомі імена, жодних шляхів із клієнта
    if name not in APP_FILES:
        return Response(status_code=404)
    return FileResponse(BASE / "static" / "app" / name, media_type=APP_FILES[name])


FONT_STACK = "Noto Sans Regular"   # єдиний шрифт, що лежить у static/vendor/fonts
GLYPH_RANGE = re.compile(r"^\d{1,5}-\d{1,5}$")


@app.get("/static/fonts/{stack}/{glyph_range}.pbf", include_in_schema=False)
def font_glyphs(stack: str, glyph_range: str) -> Response:   # підписи на карті без жодного зовнішнього запиту
    path = BASE / "static" / "vendor" / "fonts" / FONT_STACK / f"{glyph_range}.pbf"
    if stack != FONT_STACK or not GLYPH_RANGE.match(glyph_range):
        return Response(status_code=404)
    headers = {"Cache-Control": "public, max-age=86400"}
    if not path.is_file():
        # Діапазону немає в збережених: віддаємо порожній, але ВАЛІДНИЙ файл (символів просто не буде). 404 тут ламає
        # весь шар підписів: MapLibre не добудовує тайл, доки не отримає відповідь на кожен гліф.
        return Response(b"", media_type="application/x-protobuf", headers=headers)
    return FileResponse(path, media_type="application/x-protobuf", headers=headers)


WEBFONT_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(css|woff2)$")


@app.get("/static/webfonts/{name}", include_in_schema=False)
def webfont(name: str) -> Response:   # шрифти інтерфейсу теж свої: жодних запитів до Google
    path = BASE / "static" / "vendor" / "webfonts" / name
    if not WEBFONT_NAME.match(name) or not path.is_file():
        return Response(status_code=404)
    kind = "text/css" if name.endswith(".css") else "font/woff2"
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/map/paris.pmtiles", dependencies=[Depends(require_role("viewer"))], include_in_schema=False)
def map_pmtiles(rt: RuntimeDep) -> Response:
    """Власна карта одним файлом. Браузер читає її діапазонами (Range), це вміє FileResponse. Потрібен вхід: за самим
    файлом видно, яку ділянку покриває система."""
    if rt.map_file is None:
        return Response(status_code=404)
    return FileResponse(rt.map_file, media_type="application/octet-stream", headers={"Cache-Control": "private, max-age=3600"})


def map_mode(rt: Runtime) -> str:
    """offline: власний файл; online: сторонні тайли (лише з дозволу в конфігу); none: без підкладки, лише охоронні шари."""
    return "offline" if rt.map_file is not None else "online" if rt.map_online else "none"


@app.get("/api/config", dependencies=[Depends(require_role("viewer"))])
def get_config(rt: RuntimeDep) -> dict[str, Any]:
    return {**rt.engine.config(), "demo": rt.demo, "map": {"mode": map_mode(rt)},
            "editable": rt.demo_src is not None, "sensor_types": layout.type_catalog(), "max_sensors": layout.MAX_SENSORS,
            "layout_limit_m": layout.LIMIT_M, "zone_rule": {"buffer": layout.BUFFER_FRAC, "critical": layout.CRITICAL_FRAC}}


def apply_layout(rt: Runtime, sensors: list[Sensor]) -> dict[str, Any]:
    """Підміна розстановки в демо: зони виводяться з нових радарів. Без await між кроками: цикл сканів іде в тому ж потоці
    й не побачить напіврозстановку."""
    if rt.demo_src is None:
        raise HTTPException(409, "розстановка датчиків доступна лише в демо-режимі: у бойовому датчики мають ключі й задаються конфігом")
    rt.demo_src.set_sensors(sensors)
    rt.engine.set_sensors(sensors, layout.derive_zones(sensors))
    return {**rt.engine.config(), "demo": rt.demo}


async def audited_change[T](conn: HTTPConnection, ident: Identity, action: str, target: str, change: Callable[[], T]) -> T:
    """Зміна стану з журналом: спершу `started` (не записалось, дії немає: 503), потім дія, потім `ok` чи `error`.
    Результат пишеться «як вийде»: дія вже відбулася, і відкотити її через збій журналу було б гірше (стан наполовину).
    Запис у SQLite іде в потоці, щоб не блокувати цикл подій; сама зміна `change()` лишається в циклі, бо рушій живе в ньому."""
    def entry(result: str) -> AuditEntry:
        return AuditEntry(ident.user, ident.role, action, target, result, client_ip(conn))
    await asyncio.to_thread(audit_or_503, conn, entry("started"))
    try:
        done = change()
    except Exception:   # зокрема HTTPException 409: спроба була, і журнал це показує
        await asyncio.to_thread(audit_best_effort, conn, entry("error"))
        raise
    await asyncio.to_thread(audit_best_effort, conn, entry("ok"))
    return done


@app.put("/api/layout", dependencies=[Depends(json_post_guard)])
async def put_layout(ident: AdminDep, request: Request, body: layout.Layout, rt: RuntimeDep) -> dict[str, Any]:
    """Нова розстановка (повний список датчиків): додати, пересунути чи прибрати датчик це одна атомарна заміна."""
    return await audited_change(request, ident, "layout_save", f"sensors={len(body.sensors)}", lambda: apply_layout(rt, layout.build(body)))


@app.get("/api/coverage", dependencies=[Depends(require_role("viewer"))])
def get_coverage(rt: RuntimeDep) -> dict[str, Any]:
    """Покриття буферної зони поточними датчиками рушія (будь-який режим): те, що дашборд підсвічує за замовчуванням."""
    return coverage.coverage(list(rt.engine.sensors.values()), rt.engine.zones, rt.los, rt.target_alt)


@app.post("/api/coverage", dependencies=[Depends(json_post_guard)])
async def post_coverage(_: AdminDep, body: layout.Layout, rt: RuntimeDep) -> dict[str, Any]:
    """Що покриває ця розстановка в буферній зоні (клітинки з класом). Нічого не змінює: лише рахує для підсвітки в редакторі.
    Лише для admin (інструмент редактора), але в журнал не пишеться: стану не змінює, а редактор кличе його щокілька сотень мс
    під час перетягування, і журнал (та SQLite-коміти в циклі подій) потонули б у попередніх переглядах."""
    return coverage.coverage(layout.build(body), None, rt.los, rt.target_alt)


@app.delete("/api/layout", dependencies=[Depends(same_origin_guard)])
async def reset_layout(ident: AdminDep, request: Request, rt: RuntimeDep) -> dict[str, Any]:
    """Повернути вбудовані датчики демо."""
    return await audited_change(request, ident, "layout_reset", "reset", lambda: apply_layout(rt, make_sensors()))


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
            "mqtt": rt.mqtt.status() if rt.mqtt else None,
            "audit": ({"ok": rt.audit_status.ok, "rows": rt.audit_status.rows, "broken_at": rt.audit_status.broken_at}
                      if rt.audit_status else None)}   # результат старту, не перевірка зараз: verify() тримає замок рушія
    return body if scanning else JSONResponse(body, status_code=503)


@app.get("/api/sensors", dependencies=[Depends(require_role("viewer"))])
def sensors(rt: RuntimeDep) -> list[SensorStatus]:
    """Стан здоровʼя кожного датчика: OK / WAITING / LOST."""
    return list(rt.engine.sensor_status().values())


@app.get("/api/state", dependencies=[Depends(require_role("viewer"))])
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


def read_incidents(db_path: str, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    # окреме з'єднання на запит, і його треба закривати (раніше воно витікало); LIMIT -1 у SQLite = «без ліміту», тож clamp знизу
    # status=new відповідає й NULL (рядки, записані до появи рішень оператора)
    where, args = ("WHERE COALESCE(status,'new') = ?", (status,)) if status else ("", ())
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT * FROM incidents {where} ORDER BY id DESC LIMIT ?",
                          (*args, max(1, min(limit, 1000)))).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/incidents", dependencies=[Depends(require_role("viewer"))])
def incidents(rt: RuntimeDep, limit: int = 100,
              status: Literal["new", "confirmed", "dismissed"] | None = None) -> list[dict[str, Any]]:
    return read_incidents(rt.db_path, limit, status)


@app.get("/api/incidents/{incident_id}/replay", dependencies=[Depends(require_role("viewer"))])
def incident_replay(incident_id: int, rt: RuntimeDep) -> dict[str, Any]:
    """Кадри цілей навколо тривоги (див. replay.py) для відтворення на карті. Лише читання. 404: інциденту чи запису немає
    (старі інциденти, вимкнений запис, прибрані за терміном)."""
    try:
        found = read_replay(rt.db_path, incident_id)
    except (ValueError, sqlite3.Error, zlib.error) as e:   # зіпсований запис: не віддаємо напіввідновлене
        log.error("відтворення інциденту %s не прочитано: %s", incident_id, e)
        raise HTTPException(500, "Запис відтворення пошкоджено") from e
    if found is None:
        raise HTTPException(404, "Запису відтворення для цього інциденту немає")
    return found


class ReviewRequest(BaseModel):
    status: Literal["new", "confirmed", "dismissed"]
    note: str | None = Field(default=None, max_length=500)


@app.post("/api/incidents/{incident_id}/review", dependencies=[Depends(json_post_guard)])
def review_incident(incident_id: int, body: ReviewRequest, ident: OperatorDep, request: Request, rt: RuntimeDep) -> dict[str, Any]:
    """Рішення оператора (підтвердити/відхилити/повернути в new). Автор береться з сесії; нічого не пригнічує.
    Рішення й рядок журналу пишуться в одній транзакції: журнал не записався, рішення теж немає (503)."""
    entry = AuditEntry(ident.user, ident.role, "review", f"incident={incident_id} status={body.status}", "ok", client_ip(request))
    try:
        row = rt.engine.review_incident(incident_id, body.status, ident.user, body.note, audit=entry)
    except AuditError as e:
        log.error("%s", e)
        raise HTTPException(503, AUDIT_UNAVAILABLE) from e
    if row is None:
        raise HTTPException(404, "Інциденту з таким id немає")
    return row


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]]


@app.post("/api/ai/ask", response_model=None, dependencies=[Depends(json_post_guard)])
def ask_ai(ident: OperatorDep, body: AskRequest, rt: RuntimeDep) -> AskResponse | JSONResponse:
    """Чат-агент оператора: читає живий стан і минулі інциденти через тули, нічого не пише в систему (і в журнал дій теж:
    лише читання, і запитів багато). 503, якщо AI вимкнено чи недоступний — трекінг дронів від цього не залежить.

    Ключ ліміту запитів — ім'я з require_role (уже перевірена сесія), а не сире значення cookie: інакше клієнт
    обходив би ліміт, просто надсилаючи щоразу нове значення dw_session."""
    if rt.ai is None:
        return JSONResponse({"detail": "AI-асистент вимкнено або недоступний"}, status_code=503)
    if wait := rt.ai.limiter.attempt(ident.user):
        raise HTTPException(429, "Забагато запитів до AI-асистента, спробуйте пізніше",
                            headers={"Retry-After": str(math.ceil(wait))})
    try:
        result = agent.ask(body.question, llm=rt.ai.llm, rt=rt, index=rt.ai.index, max_iterations=rt.ai.max_iterations)
    except AiUnavailable as e:   # лог без ключа/секретів — текст помилки формує сам AI-шар (llm.py/rag.py)
        log.warning("AI: /api/ai/ask не вдався: %s", e)
        return JSONResponse({"detail": "AI-асистент вимкнено або недоступний"}, status_code=503)
    return AskResponse(answer=result.answer, tool_calls=result.tool_calls)


@app.get("/api/audit")
def get_audit(ident: AdminDep, request: Request, rt: RuntimeDep, limit: int = 100) -> list[dict[str, Any]]:
    """Журнал дій, найновіші першими (limit 1..1000). Читання журналу саме є дією: пишеться `audit_read`, а не записалось,
    рядків не віддаємо (503)."""
    rows = rt.engine.audit.read(limit)
    audit_or_503(request, AuditEntry(ident.user, ident.role, "audit_read", f"limit={max(1, min(1000, limit))}", "ok", client_ip(request)))
    return rows


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    rt: Runtime | None = getattr(websocket.app.state, "runtime", None)
    access: DashboardAccess = getattr(websocket.app.state, "access", None) or DashboardAccess()
    try:
        ident = await asyncio.to_thread(access.identity, websocket)
    except SessionStoreError:
        await websocket.close(code=1013)   # спробуйте пізніше: сховище сесій недоступне
        return
    # до accept: відмова тут стає HTTP 403 на рукостисканні, WebSocket навіть не відкривається. Origin захищає від чужих сайтів.
    # Роль перевіряється тут явно (WebSocket не проходить через require_role): невідома роль = відмова, як і в HTTP
    if rt is None or ident is None or not has_role(ident.role, "viewer") or not dashboard_web.origin_ok(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    rt.clients[websocket] = access.token(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        rt.clients.pop(websocket, None)
