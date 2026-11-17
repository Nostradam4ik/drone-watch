"""Веб-частина автентифікації дашборда: cookie сесії, залежності FastAPI, вхід/вихід, захисні заголовки.

Що захищено: `/`, `/api/config`, `/api/state`, `/api/sensors`, `/api/incidents`, `/ws`. `/api/health` без входу віддає лише
{"ok": ...} (для healthcheck і моніторингу), повні подробиці лише після входу. `POST /api/events` не тут: його захищає ключ датчика.

Проти підроблених запитів з інших сайтів: cookie `SameSite=Strict`, вхід лише `application/json` (форма з чужого сайту так не
відправляє), заголовок `Origin`, якщо він є, має збігатися з хостом. Те саме для WebSocket (перевірка Origin при рукостисканні).
"""
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import HTTPConnection, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from dashboard_auth import MAX_PASSWORD_LEN, LoginLimiter, SessionStore, Users

log = logging.getLogger("dronewatch.dashboard")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "dw_session"
ANONYMOUS = "(без входу)"   # ім'я користувача, коли автентифікацію вимкнено


@dataclass(frozen=True, slots=True)
class DashboardAccess:
    """Без аргументів автентифікація вимкнена (демо, розробка); з ними вхід обов'язковий."""
    users: Users | None = None
    sessions: SessionStore | None = None
    limiter: LoginLimiter | None = None

    @property
    def enabled(self) -> bool:
        return self.sessions is not None

    def token(self, conn: HTTPConnection) -> str | None:
        return conn.cookies.get(COOKIE)

    def user(self, conn: HTTPConnection) -> str | None:
        """Ім'я користувача з чинної сесії; None, якщо входу не було чи сесія скінчилась."""
        if self.sessions is None:
            return ANONYMOUS
        session = self.sessions.get(self.token(conn))
        return session.user if session else None

    def session_valid(self, token: str | None) -> bool:
        return self.sessions is None or self.sessions.get(token) is not None


def get_access(conn: HTTPConnection) -> DashboardAccess:
    return getattr(conn.app.state, "access", None) or DashboardAccess()


def origin_ok(conn: HTTPConnection) -> bool:
    """Origin, якщо він є (браузер надсилає його для WebSocket і POST), має збігатися з хостом. Без Origin (curl, скрипти) можна."""
    origin = conn.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == conn.headers.get("host")


def current_user(conn: HTTPConnection) -> str | None:
    return get_access(conn).user(conn)


def require_user(user: Annotated[str | None, Depends(current_user)]) -> str:
    if user is None:
        raise HTTPException(401, "Потрібен вхід", headers={"WWW-Authenticate": "Session"})
    return user


def json_post_guard(request: Request) -> None:
    """Для зміни стану (вхід, вихід): лише JSON і лише свій Origin."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Очікується Content-Type: application/json")
    if not origin_ok(request):
        raise HTTPException(403, "Запит з чужого джерела (Origin)")


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LEN)


router = APIRouter()


@router.get("/login", include_in_schema=False)
def login_page(request: Request) -> Response:
    if get_access(request).user(request) is not None:   # уже увійшли (або автентифікації немає): сюди нема чого дивитись
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


@router.post("/api/login", dependencies=[Depends(json_post_guard)])
def login(body: LoginIn, request: Request) -> Response:
    access = get_access(request)
    if access.users is None or access.sessions is None or access.limiter is None:
        raise HTTPException(404, "Автентифікацію дашборда вимкнено")
    client = request.client.host if request.client else "?"
    # Спроба резервується атомарно ДО перевірки пароля: інакше паралельний залп обходить ліміт (див. LoginLimiter)
    if wait := access.limiter.attempt(client):
        log.warning("вхід заблоковано (забагато невдалих спроб) з %s", client)
        raise HTTPException(429, "Забагато невдалих спроб, спробуйте пізніше", headers={"Retry-After": str(math.ceil(wait))})
    if not access.users.authenticate(body.username, body.password):
        log.warning("невдалий вхід: ім'я %r з %s", body.username, client)   # %r екранує переноси рядків; пароля в лозі немає
        raise HTTPException(401, "Хибний логін або пароль")
    access.limiter.reset(client)
    token = access.sessions.create(body.username)
    log.info("вхід: %s з %s", body.username, client)
    response = JSONResponse({"user": body.username})
    response.set_cookie(COOKIE, token, max_age=int(access.sessions.ttl_s), httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https" or os.environ.get("COOKIE_SECURE") == "1")
    return response


@router.post("/api/logout", dependencies=[Depends(json_post_guard)])
def logout(request: Request) -> Response:
    access = get_access(request)
    if access.sessions is not None and access.sessions.revoke(access.token(request)):
        log.info("вихід: з %s", request.client.host if request.client else "?")
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/api/me")
def me(user: Annotated[str, Depends(require_user)], request: Request) -> dict:
    return {"user": user, "auth": get_access(request).enabled}


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
