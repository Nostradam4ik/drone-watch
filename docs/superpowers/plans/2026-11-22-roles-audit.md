# Ролі та журнал дій: план реалізації

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ролі viewer < operator < admin з перевіркою на сервері для кожного маршруту та журнал дій, який лише дописується й захищений ланцюжком хешів.

**Architecture:** Роль задається в `dashboard.users[].role`, зберігається в сесії, і її перевіряє залежність FastAPI `require_role(min)`, яка стоїть на кожному маршруті (guard-тест це гарантує). Журнал лежить у `audit.py`: таблиця `audit_log` у тій самій SQLite, що й інциденти, тригери забороняють UPDATE і DELETE, а кожен рядок містить SHA-256 від попереднього. Рушій володіє і з'єднанням, і журналом, тому рішення оператора та запис у журнал потрапляють в одну транзакцію.

**Tech Stack:** Python 3.12+, FastAPI, pydantic v2, sqlite3, звичайний JS (static/app), pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-22-roles-audit-design.md`

## Global Constraints

- Ролі рівно `viewer`, `operator`, `admin`; порядок `viewer < operator < admin`. Невідома роль = відмова (ніколи не дозвіл).
- `dashboard.users[].role` **обов'язкове**: без нього або з невідомим значенням конфіг не валідується (сервіс не стартує).
- Без входу (demo без користувачів або `dashboard.auth: disabled`): ім'я `ANONYMOUS` («(без входу)»), роль **viewer**.
- Права: viewer читає (`/`, `/map/paris.pmtiles`, `GET /api/config|coverage|sensors|state|incidents`, `/api/me`, `/ws`); operator ще `POST /api/incidents/{id}/review`, `POST /api/ai/ask`; admin ще `PUT/DELETE /api/layout`, `POST /api/coverage`, `GET /api/audit`.
- Без ролі (allowlist guard-тесту): `/login`, `POST /api/login`, `POST /api/logout`, `POST /api/events` (ключ датчика), `GET /api/health`, `/static/*`.
- Дії журналу: `login`, `login_failed`, `login_blocked`, `logout`, `review`, `layout_save`, `layout_reset`, `coverage_set`, `denied`, `audit_read`. AI-запити не пишемо.
- Результати: `ok`, `started`, `error`, `denied`, `failed`.
- Довжини після очищення: user 64, role 16, action 32, target 200, ip 64; керівні символи (категорії Unicode Cc і Cf) вилучаються.
- Хеш: `sha256((prev_hash + "\n" + json.dumps([ts, user, role, action, target, result, ip], ensure_ascii=False, separators=(",", ":"))).encode()).hexdigest()`; `GENESIS = "0" * 64`.
- Збій запису в журнал перед зміною стану: дію не виконувати, відповідь 503 `"Журнал дій недоступний"`.
- `GET /api/audit?limit=`: за замовчуванням 100, обмеження `1..1000`, найновіші першими.
- `X-Forwarded-For` не читати; IP = `request.client.host` або `"?"`.
- Нових залежностей не додавати.
- Перевірки перед кожним комітом: `python -m ruff check .`, `python -m mypy`, `python -m pytest -q`.

## Review Focus

1. **Спроба обійти роль через WebSocket або маршрут без перевірки.** Guard-тест (Task 3) обходить усі маршрути; окремий тест: viewer відкриває `/ws` і отримує лише те саме, що й раніше (без нових полів із журналом).
2. **Сесія з роллю, якої більше немає** (сесію створено вручну з `role="root"`). `require_role` дає 403, не 500, і пише `denied` (Task 3, `test_unknown_session_role_is_denied`).
3. **Флуд невдалих входів роздуває журнал.** `login_failed` записується не частіше, ніж дозволяє `LoginLimiter`; 429 дають щонайбільше один `login_blocked` на адресу за вікно (Task 3, `test_blocked_logins_are_aggregated`).
4. **Ворожі символи в імені чи коментарі** (`\n`, `\x1b`, U+202E). У БД вони не потрапляють, а в UI показуються лише через `textContent` (Task 2, `test_control_chars_are_stripped`; Task 5, браузерний `test_audit_rows_are_rendered_as_text`).
5. **Збій SQLite під час дії** (файл read-only, диск повний). Розкладка не змінюється, рішення по тривозі не записується, відповідь 503 (Task 3, `test_audit_failure_blocks_layout_change`; Task 2, `test_review_rolls_back_when_audit_fails`).

---

### Task 1: Ролі в конфігу, користувачах і сесіях

**Files:**
- Modify: `src/dronewatch/service/dashboard_auth.py` (ролі, `Users`, `Session`, `SessionStore.create`)
- Modify: `src/dronewatch/service/config.py:35-47` (`DashboardUser.role`)
- Modify: `src/dronewatch/service/keygen.py:56-93` (`--role`, обов'язковий разом із `--dashboard-user`)
- Modify: `src/dronewatch/service/app.py:194-202` (`build_dashboard_access` передає ролі)
- Modify: `deploy/config.yaml`, `src/dronewatch/service/config.example.yaml` (додати `role:`; демо-користувач `demo` → `admin`, приклад `operator` → `operator`)
- Test: `tests/test_dashboard.py`, `tests/test_auth.py` (або де тестується конфіг), `tests/test_keygen*.py` якщо є, інакше в `tests/test_dashboard.py`

**Interfaces:**
- Produces (у `dashboard_auth.py`):
  - `Role = Literal["viewer", "operator", "admin"]`, `ROLE_RANK: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}`
  - `def has_role(actual: str | None, required: Role) -> bool` (False для невідомого `actual` або None)
  - `@dataclass(frozen=True) class UserEntry: password_hash: str; role: Role`
  - `Users(entries: Mapping[str, UserEntry])`; `Users.authenticate(name, password) -> Role | None` (та сама сталочасова поведінка)
  - `Session(user: str, role: str, expires: float)`; `SessionStore.create(user: str, role: Role) -> str`
- Produces (у `config.py`): `DashboardUser.role: Role` без значення за замовчуванням.

- [ ] **Step 1: Write the failing tests**

```python
def test_has_role_orders_roles_and_rejects_unknown():
    assert has_role("admin", "operator") and has_role("operator", "operator")
    assert not has_role("viewer", "operator")
    assert not has_role("root", "viewer") and not has_role(None, "viewer") and not has_role("", "viewer")

def test_authenticate_returns_role_or_none():
    users = Users({"alice": UserEntry(FAST, "operator")})
    assert users.authenticate("alice", PASSWORD) == "operator"
    assert users.authenticate("alice", "wrong-password!") is None
    assert users.authenticate("bob", PASSWORD) is None

def test_session_carries_role():
    store = SessionStore(60)
    assert store.get(store.create("alice", "viewer")).role == "viewer"

def test_config_user_without_role_is_rejected():
    with pytest.raises(ValidationError, match="role"):
        DashboardUser(name="a", password_hash=FAST)

def test_config_user_with_unknown_role_is_rejected():
    with pytest.raises(ValidationError):
        DashboardUser(name="a", password_hash=FAST, role="root")

def test_keygen_requires_role_and_prints_it(capsys, monkeypatch):
    monkeypatch.setenv("PW", "x" * 12)
    assert keygen.main(["--dashboard-user", "a", "--password-env", "PW"]) != 0          # без --role відмова
    assert keygen.main(["--dashboard-user", "a", "--password-env", "PW", "--role", "operator"]) == 0
    assert "role: operator" in capsys.readouterr().out

def test_shipped_configs_validate():
    for p in ("deploy/config.yaml", "src/dronewatch/service/config.example.yaml"):   # приклад без хешу пропустити, якщо він плейсхолдер
        ...
```

(`FAST` і `PASSWORD` беруться з наявних фікстур `tests/test_dashboard.py`. Для `config.example.yaml` достатньо перевірити, що в кожного користувача є `role`, бо хеш там є плейсхолдером. Подивіться, як `main` у keygen приймає argv: якщо параметра argv немає, додайте `argv: list[str] | None = None`.)

- [ ] **Step 2: Run to verify they fail**
Run: `python -m pytest tests/test_dashboard.py -q -k "role or keygen or shipped"`
Expected: FAIL (`has_role`/`UserEntry` не існують).

- [ ] **Step 3: Implement** the interfaces above. `Users` будує dummy-хеш як і зараз. Для невідомого імені `authenticate` повертає None після перевірки з dummy. Keygen друкує `  - {name: N, role: R, password_hash: "..."}`. `build_dashboard_access` будує `Users({u.name: UserEntry(u.password_hash, u.role) for u in ...})`. Оновіть усі наявні виклики `Users({...: FAST})` і `sessions.create("alice")` у тестах, щоб передавали роль (`"admin"`, якщо тест перевіряє не ролі).

- [ ] **Step 4: Run all checks**
Run: `python -m ruff check . && python -m mypy && python -m pytest -q`
Expected: усе зелене.

- [ ] **Step 5: Commit** `feat(auth): dashboard roles in config, users and sessions (role is mandatory)`

---

### Task 2: Журнал дій (`audit.py`) і рішення оператора в одній транзакції

**Files:**
- Create: `src/dronewatch/service/audit.py`
- Modify: `src/dronewatch/service/engine.py:164-175` (створити `self.audit`), `:435-455` (`review_incident` пише журнал у тій самій транзакції)
- Test: `tests/test_audit.py` (новий), `tests/test_review.py`

**Interfaces:**
- Produces (у `audit.py`):
  - `GENESIS: str`, `class AuditError(Exception)`
  - `@dataclass(frozen=True) class AuditEntry: user: str; role: str; action: str; target: str; result: str; ip: str`
  - `def clean(text: object, limit: int) -> str` (str(), вилучити Cc/Cf, обрізати)
  - `class AuditLog`:
    - `__init__(self, db: sqlite3.Connection, lock: threading.Lock, clock: Callable[[], float] = time.time)`: створює таблицю `audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, role TEXT, action TEXT, target TEXT, result TEXT, ip TEXT, prev_hash TEXT, hash TEXT)`, індекс по `ts` і тригери `audit_no_update`/`audit_no_delete` (`BEFORE UPDATE`/`BEFORE DELETE` → `SELECT RAISE(ABORT, 'audit_log is append-only')`)
    - `append(self, entry: AuditEntry) -> int`: бере lock, пише й комітить; будь-яка помилка sqlite → `AuditError` (після rollback)
    - `append_locked(self, entry: AuditEntry) -> int`: без lock і без commit (для транзакцій, які вже тримає виклик)
    - `read(self, limit: int = 100) -> list[dict[str, Any]]`: найновіші першими, limit обмежений `1..1000`
    - `verify(self) -> VerifyResult`, де `@dataclass(frozen=True) class VerifyResult: ok: bool; rows: int; broken_at: int | None; head: str`
    - `allow_blocked_record(self, key: str, window_s: float) -> bool`: True не частіше одного разу за вікно на ключ; словник обмежений 10 000 ключами (як `LoginLimiter`)
- Produces (у `engine.py`): `Engine.audit: AuditLog`; `Engine.review_incident(incident_id, status, by, note=None, *, audit: AuditEntry | None = None)`. Якщо `audit` передано, запис іде через `append_locked` до `commit`; будь-яка помилка → rollback і `AuditError`.

- [ ] **Step 1: Write the failing tests** (`tests/test_audit.py`)

```python
def make_log():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    return db, AuditLog(db, threading.Lock(), clock=iter(range(1000, 2000)).__next__)

E = AuditEntry(user="alice", role="admin", action="layout_save", target="sensors=3", result="started", ip="127.0.0.1")

def test_append_chains_hashes_and_verify_passes():
    db, log = make_log()
    log.append(E); log.append(E)
    rows = log.read()
    assert rows[1]["prev_hash"] == GENESIS and rows[0]["prev_hash"] == rows[1]["hash"]
    assert log.verify() == VerifyResult(ok=True, rows=2, broken_at=None, head=rows[0]["hash"])

def test_update_and_delete_are_rejected_by_the_database():
    db, log = make_log(); log.append(E)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_log SET user='mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit_log")

def test_tampering_with_triggers_dropped_is_detected():
    db, log = make_log(); log.append(E); log.append(E); log.append(E)
    db.execute("DROP TRIGGER audit_no_update"); db.execute("UPDATE audit_log SET user='mallory' WHERE id=2"); db.commit()
    assert log.verify().ok is False and log.verify().broken_at == 2

def test_deleted_middle_row_is_detected():
    db, log = make_log(); [log.append(E) for _ in range(3)]
    db.execute("DROP TRIGGER audit_no_delete"); db.execute("DELETE FROM audit_log WHERE id=2"); db.commit()
    assert log.verify().broken_at == 3

def test_control_chars_are_stripped():
    db, log = make_log()
    log.append(AuditEntry("a\nb\x1b[31m‮", "admin", "login_failed", "x" * 500, "failed", "1.2.3.4"))
    row = log.read()[0]
    assert row["user"] == "ab[31m" and len(row["target"]) == 200

def test_append_failure_raises_audit_error():
    db, log = make_log(); db.close()
    with pytest.raises(AuditError):
        log.append(E)

def test_read_limit_is_clamped():
    db, log = make_log(); [log.append(E) for _ in range(3)]
    assert len(log.read(0)) == 1 and len(log.read(10_000)) == 3

def test_blocked_record_once_per_window():
    clock = iter([0, 1, 400]).__next__
    log = AuditLog(sqlite3.connect(":memory:"), threading.Lock(), clock=clock)
    assert log.allow_blocked_record("1.2.3.4", 300) is True
    assert log.allow_blocked_record("1.2.3.4", 300) is False
    assert log.allow_blocked_record("1.2.3.4", 300) is True
```

У `tests/test_review.py`:

```python
def test_review_writes_audit_in_the_same_transaction(engine_with_incident):
    eng, iid = engine_with_incident
    eng.review_incident(iid, "confirmed", "alice", "ok", audit=AuditEntry("alice", "operator", "review", f"incident={iid} status=confirmed", "ok", "?"))
    assert eng.audit.read()[0]["action"] == "review"

def test_review_rolls_back_when_audit_fails(engine_with_incident, monkeypatch):
    eng, iid = engine_with_incident
    monkeypatch.setattr(eng.audit, "append_locked", Mock(side_effect=sqlite3.OperationalError("disk full")))
    with pytest.raises(AuditError):
        eng.review_incident(iid, "confirmed", "alice", audit=AuditEntry("alice", "operator", "review", "x", "ok", "?"))
    assert read_status(eng, iid) in (None, "new")      # рішення не записалось
```

(Використайте наявні фікстури з `tests/test_review.py`, що створюють рушій з інцидентом.)

- [ ] **Step 2: Run to verify they fail**
Run: `python -m pytest tests/test_audit.py tests/test_review.py -q`
Expected: FAIL (`dronewatch.service.audit` не існує).

- [ ] **Step 3: Implement** `audit.py` і зміни в рушії за інтерфейсом. `verify` іде по `ORDER BY id`, перераховує хеш і перевіряє `prev_hash == попередній hash`; `broken_at` = id першого рядка, що не зійшовся. Очищення відбувається в `append`/`append_locked`, а хешуються вже очищені значення.

- [ ] **Step 4: Run all checks**
Run: `python -m ruff check . && python -m mypy && python -m pytest -q`

- [ ] **Step 5: Commit** `feat(audit): append-only hash-chained action log; review + audit in one transaction`

---

### Task 3: `require_role` на кожному маршруті, журнал для кожної дії, `/api/audit`

**Files:**
- Modify: `src/dronewatch/service/dashboard_web.py` (`Identity`, `require_role`, вхід/вихід із журналом, `/api/me` з роллю, анонім = viewer)
- Modify: `src/dronewatch/service/app.py` (`build_dashboard_access` для demo, усі `require_user` → `require_role(...)`, журнал для layout/coverage/review, `GET /api/audit`, перевірка ролі для `/ws`)
- Modify: `deploy/config.demo.yaml` (коментар: без користувачів демо лише для перегляду; як додати admin-користувача для редагування розкладки)
- Create: `tests/helpers_auth.py` (або фікстура в `tests/conftest.py`): `login_as(client, role, user="alice") -> None`
- Test: `tests/test_roles.py` (новий), оновити `tests/test_layout.py`, `test_coverage.py`, `test_sector.py`, `test_catalog.py`, `test_review_api.py`, `test_ai_endpoint.py`, `test_static.py`, `test_health.py`, `test_dashboard.py`

**Interfaces:**
- Consumes: Task 1 (`has_role`, `Role`, `Session.role`, `SessionStore.create(user, role)`), Task 2 (`AuditLog`, `AuditEntry`, `AuditError`, `Engine.audit`, `review_incident(..., audit=)`).
- Produces (у `dashboard_web.py`):
  - `@dataclass(frozen=True) class Identity: user: str; role: str`
  - `DashboardAccess.identity(conn) -> Identity | None` (без сесій: `Identity(ANONYMOUS, "viewer")`); `DashboardAccess.user()` лишається як обгортка
  - `def require_role(min_role: Role) -> Callable[..., Identity]`; повернута функція має атрибут `dw_min_role = min_role` (на нього дивиться guard-тест). 401, якщо identity нема. Якщо ролі не вистачає: запис `denied` (target = `f"{method} {path} needs {min_role}"`; помилка журналу при цьому лише логується) і 403 `"Недостатньо прав"`.
  - `def client_ip(conn: HTTPConnection) -> str`
  - `def audit_or_503(conn: HTTPConnection, entry: AuditEntry) -> None` (AuditError → HTTPException(503, "Журнал дій недоступний"))
- Produces (у `app.py`): `GET /api/audit?limit=` → `list[dict]` з `AuditLog.read` (admin; після читання пише `audit_read`).

Правила, які тут реалізуються:
- `build_dashboard_access`: demo **з** `dashboard.users` вимагає входу, як і live; demo без користувачів і `auth: disabled` дають анонімного viewer (з попередженням у лозі). Валідація конфігу для demo без змін.
- Вхід: якщо 429 і `allow_blocked_record(ip, limiter.window_s)`, пишемо `login_blocked`. Невдалий вхід → `login_failed` (user = спробуване ім'я); якщо цей запис не вдався, лише лог, відповідь 401 як і раніше. Успіх: **спершу** `audit_or_503(login)`, потім сесія. Вихід: спершу відкликаємо сесію, потім `logout` (помилка журналу лише логується: вихід завжди спрацьовує).
- `PUT/DELETE /api/layout`: `audit_or_503(... result="started")` → дія → `append` `ok` або `error` (на HTTPException 409 теж `error`, і виняток пробрасується далі). `POST /api/coverage`: лише `audit_or_503(coverage_set, ok)` (ендпоінт нічого не змінює, але це дія admin). target для розкладки: `f"sensors={len(body.sensors)}"`, для скидання `"reset"`.
- Review: `review_incident(..., audit=AuditEntry(user, role, "review", f"incident={id} status={status}", "ok", ip))`; AuditError → 503.
- `/ws`: identity обов'язкова й `has_role(identity.role, "viewer")`; інакше закрити з кодом 1008 до accept.
- `/api/me` → `{"user", "role", "auth"}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_roles.py`)

```python
ROUTE_ALLOWLIST = {("GET", "/login"), ("POST", "/api/login"), ("POST", "/api/logout"), ("POST", "/api/events"), ("GET", "/api/health")}

def test_every_route_declares_a_role():
    missing = []
    for r in app.app.routes:
        if isinstance(r, APIRoute) and not r.path.startswith("/static/"):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                if (m, r.path) not in ROUTE_ALLOWLIST and not has_role_dependency(r.dependant):
                    missing.append((m, r.path))
    assert missing == []

MATRIX = [  # (method, path, body, мінімальна роль)
    ("GET", "/api/state", None, "viewer"), ("GET", "/api/incidents", None, "viewer"),
    ("POST", "/api/incidents/1/review", {"status": "confirmed"}, "operator"),
    ("POST", "/api/ai/ask", {"question": "?"}, "operator"),
    ("PUT", "/api/layout", LAYOUT, "admin"), ("DELETE", "/api/layout", None, "admin"),
    ("POST", "/api/coverage", LAYOUT, "admin"), ("GET", "/api/audit", None, "admin"),
]

@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
@pytest.mark.parametrize("method,path,body,need", MATRIX)
def test_role_matrix(demo_client, role, method, path, body, need):
    login_as(demo_client, role)
    r = demo_client.request(method, path, json=body)
    if has_role(role, need):
        assert r.status_code not in (401, 403)
    else:
        assert r.status_code == 403
        assert demo_client.app.state.runtime.engine.audit.read()[0]["action"] == "denied"

def test_anonymous_is_viewer_when_auth_is_off(anon_client):
    assert anon_client.get("/api/me").json()["role"] == "viewer"
    assert anon_client.put("/api/layout", json=LAYOUT).status_code == 403

def test_unknown_session_role_is_denied(demo_client):
    token = demo_client.app.state.access.sessions.create("alice", "root")  # type: ignore[arg-type]
    demo_client.cookies.set("dw_session", token)
    assert demo_client.get("/api/state").status_code == 403

def test_login_success_failure_logout_are_audited(login_client): ...   # дії login_failed, login, logout по черзі, ip заповнено
def test_blocked_logins_are_aggregated(login_client): ...             # 20 хибних спроб → рівно один login_blocked
def test_layout_change_writes_started_then_ok(demo_client): ...
def test_audit_failure_blocks_layout_change(demo_client, monkeypatch):
    login_as(demo_client, "admin")
    before = demo_client.get("/api/config").json()["sensors"]
    monkeypatch.setattr(demo_client.app.state.runtime.engine.audit, "append", Mock(side_effect=AuditError("ro")))
    assert demo_client.put("/api/layout", json=LAYOUT).status_code == 503
    assert demo_client.get("/api/config").json()["sensors"] == before
def test_audit_failure_blocks_login(login_client, monkeypatch): ...  # 503, Set-Cookie немає
def test_audit_read_is_itself_audited(demo_client): ...
def test_ws_rejects_unknown_role(demo_client): ...                   # сесія з role="root" → рукостискання відхилено
def test_me_returns_role(demo_client): ...
```

`has_role_dependency(dependant)` рекурсивно обходить `dependant.dependencies` і шукає `getattr(d.call, "dw_min_role", None)`. Фікстури: `demo_client` (demo-рушій, `DashboardAccess` із сесіями та лімітером), `anon_client` (`DashboardAccess()`), `login_client` (зі справжніми `Users` для `alice`). Їх визначте в `tests/test_roles.py`; `login_as` винесіть у спільний модуль, бо його використовують і старі тести.

- [ ] **Step 2: Run to verify they fail**
Run: `python -m pytest tests/test_roles.py -q`
Expected: FAIL (`require_role` не існує, guard-тест знаходить маршрути без ролі).

- [ ] **Step 3: Implement** правила вище. Приберіть `require_user` з усіх маршрутів у `app.py`; `/api/me` теж отримує `require_role("viewer")`. Для `/` додайте залежність `page_identity` з `dw_min_role = "viewer"`: вона повертає `Identity | None`, а не 401, і якщо identity немає або ролі не вистачає, `index` редиректить на `/login`.

- [ ] **Step 4: Update existing tests.** Тести, які раніше змінювали розкладку з `DashboardAccess()` (`test_layout`, `test_coverage`, `test_sector`, `test_catalog`, `test_static`), тепер входять як admin через `login_as`. `test_review_api` і `test_ai_endpoint` входять як operator. Перевірки, що анонім при вимкненому вході має повний доступ, переписуються на «анонім = viewer».

- [ ] **Step 5: Run all checks**
Run: `python -m ruff check . && python -m mypy && python -m pytest -q`

- [ ] **Step 6: Commit** `feat(auth): require_role on every route, audit every action, GET /api/audit (admin)`

---

### Task 4: Перевірка цілісності: CLI, старт сервісу, health

**Files:**
- Create: `src/dronewatch/service/audit_cli.py`
- Modify: `pyproject.toml` `[project.scripts]` (`dronewatch-audit = "dronewatch.service.audit_cli:main"`)
- Modify: `src/dronewatch/service/app.py` (`lifespan`: після `build_runtime` викликати `verify()`, залогувати `INFO "журнал дій: %d записів, head=%s"` або `ERROR "журнал дій ПОШКОДЖЕНО з запису id=%d"`; результат зберегти в `Runtime.audit_status: VerifyResult | None`; `/api/health` для користувача з входом додає `"audit": {"ok", "rows", "broken_at"}`)
- Test: `tests/test_audit.py`, `tests/test_health.py`

**Interfaces:**
- Consumes: `AuditLog.verify() -> VerifyResult` (Task 2).
- Produces: `audit_cli.main(argv: list[str] | None = None) -> int`: аргументи `--verify` (обов'язковий) і `--db PATH` (за замовчуванням `app.default_db_path()`). Друкує `OK: N записів, head=<hash>` → 0; `ПОШКОДЖЕНО: перший невідповідний запис id=K` → 1; файлу немає → повідомлення й 2. Відкриває БД **лише для читання** (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`) і не створює таблиць: для цього `AuditLog` отримує параметр `create: bool = True`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cli_verify_ok_and_broken(tmp_path, capsys):
    path = tmp_path / "i.db"
    db = sqlite3.connect(path); log = AuditLog(db, threading.Lock()); log.append(E); log.append(E); db.commit()
    assert audit_cli.main(["--verify", "--db", str(path)]) == 0 and "OK: 2" in capsys.readouterr().out
    db.execute("DROP TRIGGER audit_no_update"); db.execute("UPDATE audit_log SET ip='x' WHERE id=1"); db.commit()
    assert audit_cli.main(["--verify", "--db", str(path)]) == 1 and "id=1" in capsys.readouterr().out

def test_cli_missing_db(tmp_path):
    assert audit_cli.main(["--verify", "--db", str(tmp_path / "none.db")]) == 2

def test_cli_does_not_modify_db(tmp_path): ...   # mtime і вміст файлу ті самі до й після

def test_health_reports_audit_for_logged_in_user(...): ...   # "audit": {"ok": True, ...}; без входу поля немає
```

- [ ] **Step 2: Run to verify they fail** `python -m pytest tests/test_audit.py tests/test_health.py -q`
- [ ] **Step 3: Implement** за інтерфейсом. Якщо журнал пошкоджено, сервіс **все одно стартує**: захист від дронів не має зупинятись через журнал, але `ERROR` у лозі й `audit.ok=false` у health видно.
- [ ] **Step 4: Run all checks** (`ruff`, `mypy`, `pytest -q`)
- [ ] **Step 5: Commit** `feat(audit): dronewatch-audit --verify, chain check at startup, audit status in health`

---

### Task 5: UI за роллю і вкладка «Журнал дій»

**Files:**
- Modify: `src/dronewatch/service/static/app/core.js` (глобальна `me` і `const can = r => (ROLE_RANK[me && me.role] ?? -1) >= ROLE_RANK[r]` з `ROLE_RANK = {viewer: 0, operator: 1, admin: 2}`)
- Modify: `src/dronewatch/service/static/app/main.js` (`me` зберігається глобально; `edit-sensors` видно лише при `cfg.editable && can("admin")`; форма AI лише при `can("operator")`; роль показується біля імені)
- Modify: `src/dronewatch/service/static/app/panels.js` (`reviewHtml`: кнопки лише при `can("operator")`, інакше лише підсумок рішення; 403 від review показує тост «Недостатньо прав»)
- Modify: `src/dronewatch/service/static/app/editor.js` (drag/drop і commit лише при `can("admin")`)
- Create: `src/dronewatch/service/static/app/audit.js` (вкладка для admin: таблиця `/api/audit?limit=200`, оновлюється при відкритті; рядок цілісності з `/api/health` → `audit.ok`; **усі значення лише через `textContent`**); додати в `APP_FILES` (`app.py:328`) і в `index.html` перед `main.js`
- Modify: `src/dronewatch/service/static/index.html` (контейнер вкладки, прихований за замовчуванням)
- Test: `tests/test_browser.py`

**Interfaces:**
- Consumes: `/api/me → {user, role, auth}`, `GET /api/audit`, `/api/health.audit` (Tasks 3-4).

- [ ] **Step 1: Write the failing browser tests.** Фікстура `server` пише тимчасовий demo-конфіг із трьома користувачами (`viewer`, `operator`, `admin`; хеші з `hash_password` з одним паролем довжиною не менше 12 символів) і запускає сервіс із ним. Фікстура `dash_as(role)` входить через `POST /api/login` у контексті браузера, а далі працює як наявна `dash`. Наявні тести переходять на `dash_as("operator")` (або `admin` для `#edit-sensors`).

```python
def test_viewer_sees_no_review_buttons_editor_or_ai(dash_as):
    d = dash_as("viewer"); d.push(d.state(alerts=[alert(1, 1)]))
    expect(d.page.locator("button[data-review]")).to_have_count(0)
    expect(d.page.locator("#edit-sensors")).to_be_hidden()
    expect(d.page.locator("#ai-form")).to_be_hidden()

def test_operator_reviews_but_cannot_edit_layout(dash_as): ...   # кнопки є, #edit-sensors прихований, вкладки журналу немає
def test_admin_sees_audit_tab_with_integrity_ok(dash_as): ...     # вкладка видима, є рядок login для admin, позначка цілісності «ціла»
def test_audit_rows_are_rendered_as_text(dash_as, server): ...    # невдалий вхід з ім'ям '<img src=x onerror=alert(1)>' → у таблиці видно текст, елемента img немає
```

- [ ] **Step 2: Run to verify they fail** `python -m pytest -m browser tests/test_browser.py -q`
- [ ] **Step 3: Implement** за переліком файлів. Сервер усе одно відхиляє такі запити з 403, тож UI лише ховає недоступне.
- [ ] **Step 4: Run** `python -m pytest -m browser -q` і повний `python -m pytest -q`, `ruff`, `mypy`
- [ ] **Step 5: Commit** `feat(ui): role-aware dashboard and admin audit tab`

---

### Task 6: Документація, фінальна перевірка безпеки

**Files:**
- Modify: `README.md` (розділ про користувачів дашборда: ролі й таблиця прав, обов'язковий `role`, `--role` у keygen, анонім = viewer, `dronewatch-audit --verify`, розділ «Обмеження» зі специфікації дослівно за змістом, IP за reverse-proxy)
- Modify: `src/dronewatch/service/dashboard_web.py:1-8` і `dashboard_auth.py:1-10` (docstring-и: ролі, журнал)
- Modify: `docker-compose.yml`, якщо там є коментарі про демо-користувача без ролі

- [ ] **Step 1:** Оновити документацію. Перевірити, що `deploy/config.yaml` і `config.example.yaml` валідуються (`python -c "from dronewatch.service import config; config.load('deploy/config.yaml')"`).
- [ ] **Step 2:** Повний прогін: `python -m ruff check . && python -m mypy && python -m pytest -q && python -m pytest -m browser -q`. Усе зелене, кількість тестів більша за 338.
- [ ] **Step 3:** Огляд безпеки всього diff гілки (`/code-review` на високому рівні). Окремо пройти: кожен маршрут має роль; анонім ніде не отримує більше за viewer; журнал пишеться **до** зміни стану; жодного `innerHTML` для даних журналу; паролів і токенів у журналі немає.
- [ ] **Step 4: Commit** `docs: roles, audit log and its limits`; далі PR через superpowers:finishing-a-development-branch.
