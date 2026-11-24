"""Сесії та ліміт входів у Redis, щоб кілька процесів сервісу бачили одні й ті самі (вмикає `dashboard.session_store: redis`).

Fail-closed: збій Redis (обрив, таймаут, помилка) це `SessionStoreError`, а не «сесії нема» чи «ліміт не вичерпано». Сервіс
віддає 503; жодного запасного сховища в памʼяті немає, бо воно мовчки розділило б процеси на тих, хто бачить сесію, і тих, хто ні.
На сервері лежить лише SHA-256 від токена (як у памʼяті); час життя сесії тримає TTL ключа, Redis сам прибирає прострочені.
Інші ліміти (AI, журнал) лишаються на процес: тут лише сесії та ліміт входів."""
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Sequence
from typing import Any

from dronewatch.service.dashboard_auth import Role, Session, SessionStoreError

log = logging.getLogger("dronewatch.dashboard")
PREFIX = "dw:"
TIMEOUT_S = 2.0   # довше блокувати потік запиту немає сенсу: краще швидка відмова 503


def connect(url: str) -> Any:
    """Клієнт Redis із таймаутами; пінгує одразу: недоступний Redis не дає сервісу стартувати (fail-closed)."""
    try:
        import redis
    except ImportError as e:
        raise SessionStoreError("потрібен пакет redis: pip install 'dronewatch[redis]'") from e
    client = redis.Redis.from_url(url, socket_timeout=TIMEOUT_S, socket_connect_timeout=TIMEOUT_S, health_check_interval=30,
                                  decode_responses=True)
    try:
        client.ping()
    except redis.RedisError as e:
        raise SessionStoreError(f"Redis недоступний: {type(e).__name__}") from e   # без тексту помилки: у ньому може бути URL із паролем
    return client


def connect_sentinel(sentinels: Sequence[tuple[str, int]], master_name: str, password: str | None, ca_cert: str | None) -> Any:
    """Клієнт поточного master через Redis Sentinel (HA). Лише TLS (і до Sentinel, і до master), без відкату на відкритий текст.
    Після перемикання master клієнт сам знаходить новий: наступна команда йде вже на нього (сесії лишаються fail-closed на час
    перемикання). Недоступний Sentinel/master при старті: сервіс не стартує. Тексту помилок немає: у ньому адреси й пароль."""
    try:
        from redis.sentinel import Sentinel
    except ImportError:
        raise SessionStoreError("потрібен пакет redis: pip install 'dronewatch[redis]'") from None
    if not ca_cert:
        raise SessionStoreError("redis (Sentinel): потрібен ca_cert, без нього сервер не перевірити")
    timeouts: dict[str, Any] = {"socket_timeout": TIMEOUT_S, "socket_connect_timeout": TIMEOUT_S}
    # перевірка сертифіката й імені сервера задана явно: безпека не залежить від типових значень версії redis-py
    tls: dict[str, Any] = {"ssl": True, "ssl_ca_certs": ca_cert, "ssl_cert_reqs": "required", "ssl_check_hostname": True,
                           "password": password}
    client = Sentinel(list(sentinels), sentinel_kwargs={**tls, **timeouts}, **tls, **timeouts)
    master = client.master_for(master_name, decode_responses=True, health_check_interval=30, **timeouts)
    failed = ""
    try:
        master.ping()
    except Exception as e:   # RedisError, а також помилки TLS/файлу CA: у тексті адреси й шляхи, тож лише імʼя типу
        failed = type(e).__name__
    if failed:
        raise SessionStoreError(f"Redis (Sentinel) недоступний: {failed}")
    return master


class RedisSessionStore:
    def __init__(self, client: Any, ttl_s: float, *, prefix: str = PREFIX) -> None:
        self.ttl_s, self._r, self._prefix = ttl_s, client, prefix

    def _key(self, token: str) -> str:
        return self._prefix + "sess:" + hashlib.sha256(token.encode()).hexdigest()

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:   # будь-який збій клієнта = відмова, а не «сесії нема»
            log.error("Redis (сесії): %s", type(e).__name__)
            raise SessionStoreError("сховище сесій недоступне") from e

    def create(self, user: str, role: Role) -> str:
        token = secrets.token_urlsafe(32)
        self._call(self._r.set, self._key(token), json.dumps({"u": user, "r": role}), px=max(1, int(self.ttl_s * 1000)))
        return token

    def get(self, token: str | None) -> Session | None:
        if not token or len(token) > 256:
            return None
        raw = self._call(self._r.get, self._key(token))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return Session(str(data["u"]), str(data["r"]), time.time() + self.ttl_s)   # справжній строк тримає TTL ключа
        except (ValueError, KeyError, TypeError):
            return None   # зіпсований запис не дає доступу

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        return bool(self._call(self._r.delete, self._key(token)))


class RedisLoginLimiter:
    """Те саме, що `LoginLimiter`, але лічильник у Redis і фіксоване вікно (перша спроба відкриває вікно `window_s`)."""

    def __init__(self, client: Any, *, max_failures: int = 8, window_s: float = 300.0, prefix: str = PREFIX) -> None:
        self.max_failures, self.window_s, self._r, self._prefix = max_failures, window_s, client, prefix

    def attempt(self, key: str) -> float:
        k = self._prefix + "login:" + key
        try:
            pipe = self._r.pipeline(transaction=True)   # MULTI: вікно відкривається разом із першою спробою, без «ключа без TTL»
            pipe.set(k, 0, px=max(1, int(self.window_s * 1000)), nx=True)
            pipe.incr(k)
            pipe.pttl(k)
            _, count, pttl = pipe.execute()
            if pttl < 0:   # ключ уже був без TTL (чужий запис): без вікна він блокував би назавжди
                self._r.pexpire(k, max(1, int(self.window_s * 1000)))
                pttl = int(self.window_s * 1000)
        except Exception as e:
            log.error("Redis (ліміт входів): %s", type(e).__name__)
            raise SessionStoreError("сховище сесій недоступне") from e
        return 0.0 if count <= self.max_failures else max(pttl, 1) / 1000

    def reset(self, key: str) -> None:
        try:
            self._r.delete(self._prefix + "login:" + key)
        except Exception as e:
            log.error("Redis (ліміт входів): %s", type(e).__name__)
            raise SessionStoreError("сховище сесій недоступне") from e
