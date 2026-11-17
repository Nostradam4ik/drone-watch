"""Автентифікація дашборда: логін і пароль, далі сесія в HttpOnly-cookie.

Захищає читання стану, тривог і WebSocket. Це окремо від автентифікації датчиків (ключ на датчик, `auth.py`): датчик не
вміє входити в дашборд, а оператор не вміє слати події.

- Паролі зберігаються лише як scrypt-хеш із випадковою сіллю (`keygen.py --dashboard-user`), у конфігу пароля немає.
- Перевірка сталочасова й однакова за тривалістю для відомого та невідомого імені (інакше за часом відповіді видно, які імена існують).
- Сесія: довгий випадковий токен у cookie; на сервері лежить лише SHA-256 від нього, тож дамп памʼяті чи лог не дають готової сесії.
- Підбір паролів обмежує `LoginLimiter` (за адресою клієнта).
- Сесії в памʼяті одного процесу: перезапуск сервісу виходить усіх. Для кількох процесів потрібне спільне сховище."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MIN_PASSWORD_LEN = 12        # keygen.py не видасть хеш для коротшого пароля
MAX_PASSWORD_LEN = 256       # довший відкидаємо ще до хешування: інакше великі «паролі» стали б дешевою атакою на CPU
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1   # ~16 МБ памʼяті й десятки мс на перевірку
_LIMITS = {"n": 2**20, "r": 16, "p": 4}      # стеля для параметрів із хеша: зіпсований конфіг не має вимагати гігабайтів
MAX_SESSIONS = 1000


@dataclass(frozen=True, slots=True)
class ParsedHash:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes


def _scrypt(password: str, *, n: int, r: int, p: int, salt: bytes, length: int) -> bytes:
    # scrypt потребує ~128*n*r байт; запас над типовою стелею OpenSSL у 32 МБ потрібен лише для великих n
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=128 * n * (r + p) + (1 << 20))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """`scrypt$N$r$p$сіль$хеш` (base64). Мінімальну довжину пароля перевіряє виклик, а не ця функція."""
    salt = salt or secrets.token_bytes(16)
    digest = _scrypt(password, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, salt=salt, length=32)
    salt_b64, digest_b64 = base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${digest_b64}"


def parse_hash(encoded: str) -> ParsedHash:
    """ValueError, якщо рядок не у форматі hash_password або параметри поза розумними межами."""
    match encoded.split("$"):
        case ["scrypt", n, r, p, salt, digest]:
            parsed = ParsedHash(int(n), int(r), int(p), base64.b64decode(salt, validate=True), base64.b64decode(digest, validate=True))
        case _:
            raise ValueError("очікується scrypt$N$r$p$сіль$хеш")
    n_ok = 2 <= parsed.n <= _LIMITS["n"] and parsed.n & (parsed.n - 1) == 0   # степінь двійки
    if not (n_ok and 1 <= parsed.r <= _LIMITS["r"] and 1 <= parsed.p <= _LIMITS["p"]):
        raise ValueError("параметри scrypt поза допустимими межами (N степінь двійки)")
    if len(parsed.salt) < 8 or len(parsed.digest) < 16:
        raise ValueError("сіль чи хеш закороткі")
    return parsed


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > MAX_PASSWORD_LEN:
        return False
    h = parse_hash(encoded)
    return hmac.compare_digest(_scrypt(password, n=h.n, r=h.r, p=h.p, salt=h.salt, length=len(h.digest)), h.digest)


class Users:
    """Імена й хеші паролів. `authenticate` працює однаково довго для існуючого й неіснуючого імені."""

    def __init__(self, hashes: Mapping[str, str]) -> None:
        self._hashes = dict(hashes)
        self._dummy = hash_password(secrets.token_urlsafe(16))   # для вирівнювання часу, коли імені немає

    def authenticate(self, name: str, password: str) -> bool:
        encoded = self._hashes.get(name)
        verified = verify_password(password, encoded or self._dummy)
        return encoded is not None and verified


@dataclass(slots=True)
class Session:
    user: str
    expires: float


class SessionStore:
    """Сесії в памʼяті. Токен віддається клієнтові один раз; тут ключ це SHA-256 від токена."""

    def __init__(self, ttl_s: float, *, clock: Callable[[], float] = time.monotonic, max_sessions: int = MAX_SESSIONS) -> None:
        self.ttl_s, self._clock, self._max = ttl_s, clock, max_sessions
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._items = {k: s for k, s in self._items.items() if s.expires > now}
            while len(self._items) >= self._max:   # стеля памʼяті: витісняємо найстарішу
                self._items.pop(min(self._items, key=lambda k: self._items[k].expires))
            self._items[self._key(token)] = Session(user, now + self.ttl_s)
        return token

    def get(self, token: str | None) -> Session | None:
        if not token or len(token) > 256:
            return None
        with self._lock:
            session = self._items.get(self._key(token))
            if session and session.expires <= self._clock():
                del self._items[self._key(token)]
                return None
            return session

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._items.pop(self._key(token), None) is not None


class LoginLimiter:
    """Не більше `max_failures` спроб входу за `window_s` із однієї адреси; далі відмова з підказкою, коли повторити.

    Спроба ЗАРАХОВУЄТЬСЯ ще до перевірки пароля (`attempt`), а успішний вхід її знімає (`reset`). Так має бути, бо перевірка
    пароля дорога (десятки мс), а обробники входу виконуються паралельно в пулі потоків: якби спроба рахувалась лише після
    невдачі, залп із N одночасних запитів усі разом пройшов би «перевірку ліміту» до того, як хоч одна невдача записана."""

    def __init__(self, *, max_failures: int = 8, window_s: float = 300.0, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000) -> None:
        self.max_failures, self.window_s, self._clock, self._max_keys = max_failures, window_s, clock, max_keys
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._attempts.get(key)
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q if q is not None else deque()

    def attempt(self, key: str) -> float:
        """Атомарно: або резервує спробу й повертає 0 (можна перевіряти пароль), або повертає, скільки секунд почекати."""
        with self._lock:
            now = self._clock()
            q = self._recent(key, now)
            if len(q) >= self.max_failures:
                return max(0.0, q[0] + self.window_s - now)
            if key not in self._attempts and len(self._attempts) >= self._max_keys:   # стеля памʼяті: прибираємо ключі без свіжих спроб
                self._attempts = {k: r for k in list(self._attempts) if (r := self._recent(k, now))}
            self._attempts.setdefault(key, deque()).append(now)
            return 0.0

    def reset(self, key: str) -> None:
        """Успішний вхід: усі спроби цієї адреси, зокрема щойно зарезервована, перестають рахуватись."""
        with self._lock:
            self._attempts.pop(key, None)
