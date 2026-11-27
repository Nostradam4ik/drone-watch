"""Веб-частина автентифікації дашборда: cookie сесії, ролі, залежності FastAPI, вхід/вихід, журнал дій, захисні заголовки.

Кожен маршрут дашборда оголошує мінімальну роль (`require_role`, viewer < operator < admin); guard-тест валить CI, якщо
маршрут без ролі. Без входу (demo без користувачів, `dashboard.auth: disabled`) анонім має лише viewer. `/api/health` без
входу віддає лише {"ok": ...}, повні подробиці лише після входу. `POST /api/events` не тут: його захищає ключ датчика.

Журнал дій (fail-closed): вхід і кожна зміна стану спершу пишуться в журнал (`audit_or_503`); не вдалося записати, дія не
виконується (503). Відмова (`denied`), невдалий вхід і вихід пишуться «як вийде»: збій журналу лише логується, бо відповідь
від цього не стає небезпечнішою (відмова лишається відмовою, вихід усе одно спрацьовує).

Проти підроблених запитів з інших сайтів: cookie `SameSite=Strict`, вхід лише `application/json` (форма з чужого сайту так не
відправляє), заголовок `Origin`, якщо він є, має збігатися з хостом. Те саме для WebSocket (перевірка Origin при рукостисканні).
"""
import contextlib
import json
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import HTTPConnection, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from dronewatch.service.audit import AuditEntry, AuditError
from dronewatch.service.dashboard_auth import MAX_PASSWORD_LEN, LimiterLike, Role, Session, SessionStoreLike, Users, has_role, limiter_key
from dronewatch.service.ha import FencedError, NotActiveError
from dronewatch.service.store import StoreError

log = logging.getLogger("dronewatch.dashboard")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "dw_session"
ANONYMOUS = "(без входу)"   # ім'я користувача, коли автентифікацію вимкнено
ANONYMOUS_ROLE: Role = "viewer"   # без входу лише перегляд: жодна зміна стану анонімові недоступна
# Відмови (`denied`) у журнал не частіше одного рядка на адресу й користувача за це вікно: інакше будь-хто, хто може слати
# запити (без входу це будь-який клієнт), роздуває incidents.db до повного диска, і тоді 503 отримують уже оператори.
# У лог (log.warning) іде КОЖНА відмова.
DENIED_WINDOW_S = 300.0
DENIED_ROWS_PER_MIN = 30   # стеля рядків denied на всі адреси разом; понад неї один зведений рядок denied_flood за хвилину
LOGIN_FAILED_ROWS_PER_MIN = 30   # стеля рядків login_failed на всіх адрес разом; понад неї один зведений рядок за хвилину
AUDIT_UNAVAILABLE = "Журнал дій недоступний"


@dataclass(frozen=True, slots=True)
class Identity:
    """Хто робить запит: ім'я й роль із сесії (роль рядком: сесію могли створити з роллю, якої вже немає)."""
    user: str
    role: str


@dataclass(frozen=True, slots=True)
class DashboardAccess:
    """Без аргументів автентифікація вимкнена (демо, розробка); з ними вхід обов'язковий."""
    users: Users | None = None
    sessions: SessionStoreLike | None = None
    limiter: LimiterLike | None = None

    @property
    def enabled(self) -> bool:
        return self.sessions is not None

    def token(self, conn: HTTPConnection) -> str | None:
        return conn.cookies.get(COOKIE)

    def identity(self, conn: HTTPConnection) -> Identity | None:
        """Користувач і роль із чинної сесії; None, якщо входу не було чи сесія скінчилась. Без входу: анонімний viewer."""
        if self.sessions is None:
            return Identity(ANONYMOUS, ANONYMOUS_ROLE)
        return self._current(self.sessions.get(self.token(conn)))

    def _current(self, session: Session | None) -> Identity | None:
        """Сесія чинна, лише якщо користувач ще є в конфігу й роль та сама: у Redis сесії переживають перезапуск, і видалений
        чи пониженій користувач інакше діяв би до кінця TTL."""
        if session is None:
            return None
        if self.users is not None and self.users.role_of(session.user) != session.role:
            return None
        return Identity(session.user, session.role)

    def user(self, conn: HTTPConnection) -> str | None:
        """Ім'я користувача з чинної сесії; None, якщо входу не було чи сесія скінчилась."""
        ident = self.identity(conn)
        return ident.user if ident else None

    def session_valid(self, token: str | None) -> bool:
        return self.sessions is None or self._current(self.sessions.get(token)) is not None


def get_access(conn: HTTPConnection) -> DashboardAccess:
    return getattr(conn.app.state, "access", None) or DashboardAccess()


def origin_ok(conn: HTTPConnection) -> bool:
    """Origin, якщо він є (браузер надсилає його для WebSocket і POST), має збігатися з хостом. Без Origin (curl, скрипти) можна."""
    origin = conn.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == conn.headers.get("host")


def current_user(conn: HTTPConnection) -> str | None:
    """Ім'я, лише якщо сесія чинна й роль відома (для /api/health: подробиці бачить щонайменше viewer). Невідома роль = None."""
    ident = get_access(conn).identity(conn)
    return ident.user if ident is not None and has_role(ident.role, "viewer") else None


def client_ip(conn: HTTPConnection) -> str:
    """Адреса з самого з'єднання. X-Forwarded-For не читаємо: його підробляє будь-хто (за проксі це адреса проксі)."""
    return conn.client.host if conn.client else "?"


def _audit_log(conn: HTTPConnection) -> Any:
    """Журнал дій рушія або None, якщо рушія ще немає (без імпорту app.py: той сам імпортує цей модуль)."""
    runtime = getattr(conn.app.state, "runtime", None)
    return runtime.engine.audit if runtime is not None else None


def audit_or_503(conn: HTTPConnection, entry: AuditEntry) -> None:
    """Запис ДО дії: не вдалося (чи журналу немає взагалі), дія не виконується, відповідь 503."""
    audit = _audit_log(conn)
    if audit is None:
        log.error("журнал дій недоступний (рушій не ініціалізовано): дію %s відхилено", entry.action)
        raise HTTPException(503, AUDIT_UNAVAILABLE)
    try:
        conn.app.state.runtime.engine.fence()   # HA: без чинної оренди запис пішов би в памʼять, а дія пройшла б без сліду
    except (NotActiveError, FencedError, StoreError) as e:
        log.error("%s: дію %s відхилено", e, entry.action)
        raise HTTPException(503, AUDIT_UNAVAILABLE) from e
    try:
        audit.append(entry)
    except AuditError as e:
        log.error("%s: дію %s відхилено", e, entry.action)
        raise HTTPException(503, AUDIT_UNAVAILABLE) from e


def audit_best_effort(conn: HTTPConnection, entry: AuditEntry) -> None:
    """Для відмов, невдалих входів, виходу й результату вже виконаної дії: збій журналу лише в лог, відповідь не змінюється."""
    audit = _audit_log(conn)
    try:
        if audit is None:
            raise AuditError("рушій не ініціалізовано")
        audit.append(entry)
    except AuditError as e:
        log.error("не записано в журнал дій (%s): %s", entry.action, e)


def require_role(min_role: Role) -> Callable[..., Identity]:
    """Залежність маршруту: 401 без входу, 403 (і запис `denied`), якщо роль нижча чи невідома. Атрибут `dw_min_role`
    читає guard-тест: маршрут без нього валить CI."""
    def dependency(conn: HTTPConnection) -> Identity:
        ident = get_access(conn).identity(conn)
        if ident is None:
            raise HTTPException(401, "Потрібен вхід", headers={"WWW-Authenticate": "Session"})
        if not has_role(ident.role, min_role):
            method = conn.scope.get("method", "WS")
            ip = client_ip(conn)
            log.warning("відмова: %r (роль %r) %s %s, потрібна %s, з %s", ident.user, ident.role, method, conn.url.path, min_role, ip)
            audit = _audit_log(conn)
            if audit is not None and audit.allow_blocked_record(f"denied:{limiter_key(ip)}:{ident.user}", DENIED_WINDOW_S):
                if audit.take("denied", DENIED_ROWS_PER_MIN, 60.0):   # спільна стеля: зміна адрес (IPv6) не роздуває БД
                    audit_best_effort(conn, AuditEntry(ident.user, ident.role, "denied", f"{method} {conn.url.path} needs {min_role}",
                                                       "denied", ip))
                elif audit.take("denied_flood", 1, 60.0):
                    audit_best_effort(conn, AuditEntry(ident.user, ident.role, "denied_flood", "", "denied", ip))
            raise HTTPException(403, "Недостатньо прав")
        return ident
    dependency.dw_min_role = min_role  # type: ignore[attr-defined]
    dependency.__name__ = f"require_role_{min_role}"
    return dependency


def page_identity(conn: HTTPConnection) -> Identity | None:
    """Лише для сторінки `GET /`: замість 401 повертає None, і сторінка веде на вхід. Сама нічого НЕ забороняє (роль
    перевіряє обробник), тож має окрему позначку `dw_page_only`, а не `dw_min_role`: guard-тест приймає її тільки для `GET /`."""
    return get_access(conn).identity(conn)


page_identity.dw_page_only = True  # type: ignore[attr-defined]


def json_post_guard(request: Request) -> None:
    """Для зміни стану (вхід, вихід): лише JSON і лише свій Origin."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Очікується Content-Type: application/json")
    if not origin_ok(request):
        raise HTTPException(403, "Запит з чужого джерела (Origin)")


def same_origin_guard(request: Request) -> None:
    """Для зміни стану без тіла (DELETE): лише свій Origin (Content-Type тут нема чого перевіряти)."""
    if not origin_ok(request):
        raise HTTPException(403, "Запит з чужого джерела (Origin)")


same_origin_guard.dw_state_guard = True  # type: ignore[attr-defined]
json_post_guard.dw_state_guard = True  # type: ignore[attr-defined]   # позначка для guard-тесту: зміна стану має один із двох


MAX_BODY_BYTES = 1_048_576   # 1 МіБ: пачка з 1000 подій займає ~0,2 МіБ, розстановка ще менше


class BodyLimitMiddleware:
    """Тіло запиту більше за `max_bytes` відхиляється (413) ще до розбору JSON: за Content-Length одразу, а без нього
    (chunked) під час читання. Чистий ASGI, без буферизації всього тіла."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                await self._reject(send, 400, "Хибний Content-Length")
                return
            if too_big:
                await self._reject(send, 413, "Тіло запиту завелике")
                return
        seen, started, exceeded = 0, False, False

        async def counting_receive() -> Any:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Any) -> None:
            nonlocal started
            if exceeded and not started:
                return   # застосунок міг обернути збій читання у власну 400: відповідь дамо ми, 413
            started = started or message["type"] == "http.response.start"
            await send(message)

        with contextlib.suppress(_BodyTooLarge):
            await self.app(scope, counting_receive, tracking_send)
        if exceeded:
            if started:
                raise _BodyTooLarge   # відповідь уже пішла: лишається обірвати з'єднання
            await self._reject(send, 413, "Тіло запиту завелике")

    @staticmethod
    async def _reject(send: Any, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close"), *((k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items())]})
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LEN)


router = APIRouter()


@router.get("/login", include_in_schema=False)
def login_page(request: Request) -> Response:
    ident = get_access(request).identity(request)
    if ident is not None and has_role(ident.role, "viewer"):   # уже увійшли (або входу немає): сюди нема чого дивитись
        return RedirectResponse("/", status_code=303)   # сесія з невідомою роллю бачить форму, а не вічне коло / ↔ /login
    return FileResponse(STATIC / "login.html")


@router.post("/api/login", dependencies=[Depends(json_post_guard)])
def login(body: LoginIn, request: Request) -> Response:
    access = get_access(request)
    if access.users is None or access.sessions is None or access.limiter is None:
        raise HTTPException(404, "Автентифікацію дашборда вимкнено")
    client = client_ip(request)
    # У журнал і лог іде лише відоме ім'я: у полі імені люди іноді набирають пароль, а його зберігати не можна
    shown = body.username if access.users.knows(body.username) else "?"
    # Спроба резервується атомарно ДО перевірки пароля: інакше паралельний залп обходить ліміт (див. LoginLimiter)
    lkey = limiter_key(client)
    if wait := access.limiter.attempt(lkey):
        log.warning("вхід заблоковано (забагато невдалих спроб) з %s", client)
        audit = _audit_log(request)
        if audit is not None and audit.allow_blocked_record(lkey, access.limiter.window_s):   # один запис на адресу за вікно
            audit_best_effort(request, AuditEntry(shown, "", "login_blocked", "", "denied", client))
        raise HTTPException(429, "Забагато невдалих спроб, спробуйте пізніше", headers={"Retry-After": str(math.ceil(wait))})
    role = access.users.authenticate(body.username, body.password)
    if role is None:
        log.warning("невдалий вхід: ім'я %r з %s", shown, client)
        # Не частіше, ніж пропускає LoginLimiter (спроба вже зарахована вище); керівні символи з імені вилучає сам журнал
        audit = _audit_log(request)
        if audit is None or audit.take("login_failed", LOGIN_FAILED_ROWS_PER_MIN, 60.0):
            audit_best_effort(request, AuditEntry(shown, "", "login_failed", "", "failed", client))
        elif audit.take("login_failed_flood", 1, 60.0):
            # окремий лічильник: таблицю ключів за адресами (стеля 10k) він не витісняє
            audit_best_effort(request, AuditEntry("", "", "login_failed_flood", "", "denied", client))
        raise HTTPException(401, "Хибний логін або пароль")
    # Успішний вхід пишемо щоразу (без агрегації): для нього потрібен справжній пароль, а scrypt і LoginLimiter обмежують темп
    audit_or_503(request, AuditEntry(body.username, role, "login", "", "ok", client))   # спершу журнал, потім сесія
    access.limiter.reset(lkey)
    token = access.sessions.create(body.username, role)
    log.info("вхід: %s з %s", body.username, client)
    response = JSONResponse({"user": body.username})
    response.set_cookie(COOKIE, token, max_age=int(access.sessions.ttl_s), httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https" or os.environ.get("COOKIE_SECURE") == "1")
    return response


@router.post("/api/logout", dependencies=[Depends(json_post_guard)])
def logout(request: Request) -> Response:
    access = get_access(request)
    token = access.token(request)
    session = access.sessions.get(token) if access.sessions is not None else None
    # Спершу відкликаємо сесію, потім журнал: вихід спрацьовує завжди, навіть коли журнал недоступний
    if access.sessions is not None and access.sessions.revoke(token):
        log.info("вихід: з %s", client_ip(request))
        if session is not None:
            audit_best_effort(request, AuditEntry(session.user, session.role, "logout", "", "ok", client_ip(request)))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/api/me")
def me(ident: Annotated[Identity, Depends(require_role("viewer"))], request: Request) -> dict:
    return {"user": ident.user, "role": ident.role, "auth": get_access(request).enabled}


SECURITY_HEADERS = {
    "Cache-Control": "no-store",                        # стан і тривоги не мають осідати в кеші браузера чи проксі
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",                          # дашборд не вбудовують у чужі сторінки (clickjacking)
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


async def add_security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response
