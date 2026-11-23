# Активний рушій і гарячий резерв (HA) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Кілька копій сервісу, з яких пише й показує людям одна (лідер за орендою в Redis), а решта гарячі резерви з живими треками.

**Architecture:** `ha.py` (оренда + автомат ролей, нічого не знає про рушій) викликає `Engine.activate/deactivate`. Резервний `Engine` працює на `:memory:` БД, тому його побічні ефекти нешкідливі; при підвищенні він відкриває справжній файл, захоплює запобіжник `epoch` у самому файлі й дописує тривоги, підняті в паузі. Люди ходять лише на лідера: `/readyz` для балансувальника і 503 від резерву як другий рубіж.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, redis (extra `redis`), pytest.

**Spec:** [docs/superpowers/specs/2026-11-23-ha-failover-design.md](../specs/2026-11-23-ha-failover-design.md)

## Global Constraints
- `ha.enabled: false` (за замовчуванням) = поведінка й тести як раніше; новий код не виконується.
- Fail-closed: HA без Redis при старті = сервіс не стартує (як `dashboard_redis.connect`); немає чинної оренди = жодних записів у БД і дій оператора.
- `lease_ttl_s` за замовчуванням 6, поновлення кожні `ttl/3`; лідер сам відраховує термін за `time.monotonic()` від **початку** запиту на поновлення.
- Ключі Redis з префіксом `dw:`: `dw:leader` (`<instance_id>:<epoch>`), `dw:epoch` (лічильник), канал `dw:events`.
- Тривоги в паузі: список `pending` не більший за 200; при підвищенні пишуться з `failover=1`.
- Ключі датчиків у Redis не потрапляють ніколи.
- Мова коментарів, логів і повідомлень: українська, як у решти коду. Перевірки: `python -m ruff check .`, `python -m mypy`, `python -m pytest -q`.

## Review Focus
- Redis перезапущено й `dw:epoch` почався з 1, а в файлі БД уже більший `epoch`: лідер має підняти лічильник і піти далі, а не застрягти.
- Запит до Redis завис довше за `ttl`: лідер сам перестає вважати себе лідером за своїм годинником (`is_valid()` стає False), навіть якщо потік поновлення стоїть.
- Два «готових» лідери при розриві з Redis: `/readyz` каже 200 обом (свідомий компроміс з дизайну); тест лише фіксує це, а стара копія з меншим `epoch` не може писати.
- `POST /api/events` на резерв: 503, події не губляться мовчки (датчик ретраїть на лідера).
- Подія з релея з чужим датчиком чи нечислами: резерв відхиляє її тим самим `_screen`, не падає.
- `ha.enabled` у `mode: demo`: конфіг відхиляється з чіткою помилкою.
- Підвищення з порожнім `pending` і з переповненим (200+): без винятку, зайві відкинуті з логом.

## File Structure
- Create `src/dronewatch/service/ha.py`: `LeaseStore`, `RedisLeaseStore`, `Elector`, `RedisEventRelay`.
- Modify `src/dronewatch/service/config.py`: `HaCfg`.
- Modify `src/dronewatch/service/engine.py`: режим резерву, `activate/deactivate/fence`, `pending`, довірений ingest.
- Modify `src/dronewatch/service/app.py`: збірка, потік, middleware, `/readyz`, ws, health.
- Modify `deploy/mosquitto.acl`, `deploy/mosquitto.passwd`, `docker-compose.yml`, `README.md`.
- Tests: `tests/test_ha_config.py`, `test_ha_lease.py`, `test_ha_engine.py`, `test_ha_app.py`, `test_ha_failover.py`.

---

### Task 1: Конфіг `ha`

**Files:** Modify `src/dronewatch/service/config.py` (клас перед `Config`, поле `ha: HaCfg = HaCfg()`, перевірка в `_check`); Test `tests/test_ha_config.py`.

**Interfaces:** Produces `HaCfg(enabled: bool=False, redis_url: str|None=None, lease_ttl_s: float=6.0, instance_id: str="auto")`; `lease_ttl_s` у `[2, 60]`.

- [ ] **Step 1: tests.** `test_ha_off_by_default` (`Config().ha.enabled is False`); `test_ha_enabled_needs_redis_url` і `test_ha_requires_live_mode` (обидва `SystemExit`/`ValidationError` з текстом, що називає поле: `ha.redis_url`, `mode: live`); `test_ttl_bounds` (1 і 61 відхиляються).
- [ ] **Step 2:** запустити, очікувати FAIL.
- [ ] **Step 3:** додати `HaCfg` і правило в `Config._check`: при `ha.enabled` потрібні `mode == "live"` і `redis_url`.
- [ ] **Step 4:** PASS, потім `ruff` і `mypy`.
- [ ] **Step 5:** commit `feat(ha): конфіг ha`.

### Task 2: Оренда й автомат ролей

**Files:** Create `src/dronewatch/service/ha.py`; Test `tests/test_ha_lease.py`.

**Interfaces:**
- Produces:
  - `class LeaseStoreError(Exception)`.
  - `class LeaseStore(Protocol)`: `acquire(owner: str, ttl_ms: int) -> int | None` (epoch, якщо оренду взято; None, якщо тримає інший), `renew(owner: str, epoch: int, ttl_ms: int) -> bool`, `release(owner: str, epoch: int) -> None`, `raise_epoch(at_least: int) -> None`. Збій сховища це `LeaseStoreError`.
  - `class Role(StrEnum)`: `STANDBY`, `LEADER`, `DEGRADED`.
  - `class Elector`: `__init__(store, owner: str, ttl_s: float, *, clock=time.monotonic, on_promote: Callable[[int], None], on_demote: Callable[[Role], None])`; поля `role: Role`, `epoch: int | None`; `step() -> None`; `is_valid() -> bool` (LEADER і `clock() < valid_until`); `stop() -> None` (звільнити оренду).
  - `class FencedError(RuntimeError)` з полем `stored: int` і `class NotActiveError(RuntimeError)`: визначаються тут (в `ha.py`, що не імпортує рушій), `engine.py` їх імпортує; так немає циклу імпортів.

- [ ] **Step 1: tests** (фейкове сховище в пам'яті з керованим «часом» і можливістю падати):
  - `test_standby_acquires_when_free`: `step()` → `role == LEADER`, `epoch == 1`, `on_promote(1)` викликано раз.
  - `test_second_instance_stays_standby_while_held`.
  - `test_leader_renews_and_extends_deadline`.
  - `test_standby_takes_over_after_ttl`: перший не поновлює, годинник за `ttl`, другий став лідером з `epoch == 2`.
  - `test_store_error_before_deadline_keeps_leader_then_degrades_after_deadline`: `is_valid()` True до терміну, після нього False, `role == DEGRADED`, `on_demote(DEGRADED)` викликано раз.
  - `test_degraded_regains_same_epoch_when_store_returns` (`on_promote(epoch)` вдруге); `test_degraded_becomes_standby_if_other_holds`.
  - `test_is_valid_false_past_deadline_even_without_step`: потік зупинився, а `is_valid()` вже False.
  - `test_fenced_on_promote_raises_epoch_and_retries`: `on_promote` кидає `FencedError(stored=50)`; електор викликає `store.raise_epoch(50)`, лишається STANDBY, наступний `step()` дає `epoch > 50`.
  - `test_stop_releases_lease`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** реалізувати `Elector.step()`: LEADER → `t0 = clock()`, `renew`; True → `valid_until = t0 + ttl`; False → `STANDBY` + `on_demote(STANDBY)`; `LeaseStoreError` → якщо `clock() >= valid_until` → `DEGRADED` + `on_demote(DEGRADED)`. DEGRADED/STANDBY → спроба `renew` (лише DEGRADED зі старим epoch) чи `acquire`; `LeaseStoreError` лише логується. `on_promote` кидає `FencedError` → `raise_epoch(e.stored)`, залишитись STANDBY.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 3: Redis-реалізація сховища

**Files:** Modify `ha.py` (`RedisLeaseStore`, скрипти `_RENEW_LUA`, `_RELEASE_LUA`, `_RAISE_EPOCH_LUA`); Test `tests/test_ha_lease.py` (додати).

**Interfaces:** Produces `RedisLeaseStore(client, *, prefix: str = "dw:")`, що реалізує `LeaseStore`. `acquire` = `INCR dw:epoch`, потім `SET dw:leader "<owner>:<epoch>" NX PX ttl` (не вдалось → None). `renew`/`release` порівнюють значення атомарно в Lua. Будь-яка `redis.RedisError` це `LeaseStoreError` (без тексту помилки: у ньому може бути URL із паролем, як у `dashboard_redis.connect`).

- [ ] **Step 1: tests** з `FakeRedis` (за зразком `tests/test_redis_sessions.py`), де `eval(script, n, *args)` розпізнає скрипт за тотожністю з константою модуля: `test_acquire_sets_value_and_epoch`, `test_acquire_fails_when_held`, `test_renew_only_if_value_matches`, `test_release_only_if_value_matches`, `test_raise_epoch_never_lowers`, `test_redis_error_becomes_lease_store_error` (і текст помилки не містить пароля з URL).
- [ ] **Step 2:** FAIL. **Step 3:** реалізувати. **Step 4:** PASS. **Step 5:** commit.
- [ ] **Step 6 (ручна перевірка проти справжнього Redis, не в CI):** `docker run -d -p 6379:6379 redis:7`, один раз виконати той самий сценарій у короткому скрипті й побачити, що Lua працює. Результат записати в повідомленні коміту.

### Task 4: Рушій: резерв, підвищення, запобіжник

**Files:** Modify `src/dronewatch/service/engine.py`; Test `tests/test_ha_engine.py`.

**Interfaces:**
- Produces:
  - Імпортує `FencedError`, `NotActiveError` з `ha.py` (Task 2).
  - `Engine(..., standby: bool = False)`: при `standby` БД це `:memory:`, `self.active = False`.
  - `Engine.activate(db_path: str, epoch: int) -> None`; `Engine.deactivate() -> None`; `Engine.fence() -> None` (raise `NotActiveError`, якщо `not active`; raise `FencedError`, якщо `epoch` у файлі інший); `Engine.active: bool`, `Engine.epoch: int | None`, `Engine.pending: list[Alert]`.
  - Схема БД виноситься в `_open_db(path) -> sqlite3.Connection` (інкапсулює CREATE/міграції; нова колонка `failover INTEGER`; таблиця `ha_epoch(id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL)`).
- Consumes: існуючі `ReplayRecorder`, `AuditLog`.

- [ ] **Step 1: tests** (`tmp_path`):
  - `test_standby_engine_never_touches_real_file`: резерв тікає скани, файлу немає.
  - `test_activate_creates_schema_and_claims_epoch`: після `activate(path, 3)` у `ha_epoch` рядок 3, `active`.
  - `test_activate_with_lower_epoch_raises_fenced_with_stored`: файл має 5, `activate(path, 2)` → `FencedError.stored == 5`, двигун лишається неактивним.
  - `test_fence_detects_newer_leader`: після `activate(path, 3)` інше з'єднання ставить 4; `fence()` → `FencedError`.
  - `test_alerts_while_inactive_go_to_pending_not_db`: тривога в зоні (як у `tests/test_review.py`) при `standby`: `engine.pending` має її, `id == 0`.
  - `test_activate_flushes_pending_with_failover_flag`: після `activate` рядок у `incidents` з `failover = 1`, `pending` порожній, тривога в `engine.alerts` має реальний `id`.
  - `test_pending_capped_at_200`.
  - `test_review_inactive_raises_not_active`; `test_deactivate_returns_to_memory_and_blocks_writes`.
  - `test_non_ha_engine_unchanged`: `Engine(...)` без `standby` має `active is True`, `fence()` нічого не робить (ніколи не піднімає при `epoch is None`).
- [ ] **Step 2:** FAIL. **Step 3:** реалізація: `_open_db` з існуючого коду; `_evaluate` при `not self.active` складає в `pending` (без INSERT), при активному з `epoch` викликає `fence()`, а `FencedError` у `tick` → `deactivate` і тривога йде в `pending`; `review_incident` кидає `NotActiveError`, коли неактивний, і викликає `fence()`. `activate` усе виконує під `self.lock`: відкрити, `BEGIN IMMEDIATE`, порівняти/записати `epoch`, підмінити `db`/`audit`/`replay` (з тими самими `ReplayOptions`, які зберегти в `self._replay_opts`), дописати `pending`, закрити старе з'єднання.
- [ ] **Step 4:** PASS + повний `pytest -q` (не зламати існуючі тести рушія). **Step 5:** commit.

### Task 5: Довірений ingest і список для релея

**Files:** Modify `engine.py` (`ingest_detailed`, `_screen`, `IngestResult`); Test `tests/test_ha_engine.py` (додати).

**Interfaces:** Produces `Engine.ingest_detailed(events, *, trusted: bool = False)`; `IngestResult.relay: list[dict[str, Any]]` (події, що пройшли автентифікацію, **без поля `key`**; порожньо, якщо автентифікація вимкнена? ні: завжди перелік пройдених, щоб HA працювало і без auth).

- [ ] **Step 1: tests:** `test_trusted_skips_key_check` (двигун з `SensorAuth`, подія без ключа: `trusted=True` приймається, без прапорця відхиляється); `test_relay_list_has_no_keys_and_only_authenticated` (у пакеті два датчики, ключ лише одного: у `relay` одна подія, без `key`); `test_trusted_still_rejects_invalid_values` (NaN відхиляється).
- [ ] **Step 2:** FAIL. **Step 3:** реалізувати без зміни поведінки за замовчуванням. **Step 4:** PASS. **Step 5:** commit.

### Task 6: Збірка HA у сервісі

**Files:** Modify `app.py`; Test `tests/test_ha_app.py`.

**Interfaces:**
- Produces:
  - `Runtime.ha: HaState | None`, де `HaState` (dataclass): `elector: Elector`, `relay: RedisEventRelay`, `thread: threading.Thread | None`, `stop: threading.Event`.
  - `build_ha(cfg, engine, db_path, runtime_getter) -> HaState | None` (None, коли `ha.enabled` false).
  - `instance_id`: `cfg.ha.instance_id`, а `"auto"` = `socket.gethostname()`.
- Consumes: `dashboard_redis.connect(url)` (fail-closed), `Elector`, `RedisLeaseStore`, `Engine.activate/deactivate`.

- [ ] **Step 1: tests:** `test_build_runtime_ha_off_has_no_ha`; `test_build_runtime_ha_builds_standby_engine` (фейковий Redis підставляється через `monkeypatch` на `dashboard_redis.connect`; `engine.active is False`); `test_ha_start_fails_without_redis` (`connect` кидає `SessionStoreError` → старт піднімає помилку, не мовчить); `test_on_promote_activates_engine_and_runs_audit_check`; `test_on_demote_deactivates_engine`.
- [ ] **Step 2:** FAIL. **Step 3:** у `build_runtime` при HA створити `Engine(standby=True)`; `on_promote` = `engine.activate(db_path, epoch)` + `check_audit_chain`; `on_demote` = `engine.deactivate()` лише для `STANDBY` (для `DEGRADED` двигун теж стає неактивним: пише лише в `pending`). Потік: цикл `elector.step()` кожні `ttl/3` (не рідше 0.5 с), у `lifespan` старт до MQTT, на зупинці `stop.set()`, `elector.stop()`.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 7: Ворота для людей, `/readyz`, health

**Files:** Modify `app.py`; Test `tests/test_ha_app.py` (додати).

**Interfaces:** Produces `GET /readyz` (без автентифікації; 200 `{"ready": true, "role": ...}` для `LEADER` і `DEGRADED`, інакше 503 `{"ready": false, "role": "standby"}`); `ha` у тілі `/api/health` (`{"role", "epoch", "instance"}`; без входу не показується); HTTP-middleware: `STANDBY` відповідає 503 на все, крім `/readyz` і `/api/health`; `DEGRADED` відповідає 503 на `POST/PUT/DELETE/PATCH`, крім `/api/events`; ws при `STANDBY` закривається кодом 1013 до `accept`; `@app.exception_handler(NotActiveError)` і `FencedError` → 503; `audit_or_503` викликає `rt.engine.fence()` для HA-режиму.

- [ ] **Step 1: tests (TestClient + підмінений `Elector.role`):** `test_readyz_leader_200`, `test_readyz_standby_503`, `test_readyz_degraded_200`, `test_standby_blocks_state_and_events` (`/api/state` і `POST /api/events` дають 503), `test_standby_health_still_answers`, `test_degraded_allows_get_state_but_blocks_review_and_layout` (`POST /api/incidents/1/review` → 503; `GET /api/state` → 200; `POST /api/events` → 200), `test_ws_closed_for_standby`, `test_no_ha_means_no_gate` (без HA жодна відповідь не змінилась; `/readyz` у non-HA режимі повертає 200 `{"ready": true, "role": "single"}`).
- [ ] **Step 2:** FAIL. **Step 3:** реалізувати. Нові ендпоінти не зобов'язані мати `require_role`, але `tests/test_roles.py` має allowlist: додати `/readyz` туди й перевірити, що тест ролей проходить.
- [ ] **Step 4:** PASS + `pytest -q tests/test_roles.py`. **Step 5:** commit.

### Task 8: Релей HTTP-подій у резерв

**Files:** Modify `ha.py` (`RedisEventRelay`), `app.py` (`post_events`, старт потоку підписки); Test `tests/test_ha_app.py`.

**Interfaces:** Produces `RedisEventRelay(client, *, channel: str = "dw:events")` з `publish(events: list[dict[str, Any]]) -> None` (best-effort: збій логується раз за вікно, не ламає `post_events`) і `subscribe(handler: Callable[[list[dict[str, Any]]], None], stop: threading.Event) -> None` (цикл, відкидає не-JSON і не-список без винятку).

- [ ] **Step 1: tests:** `test_post_events_publishes_relay_without_keys` (канал отримав події, жодного `key`); `test_relay_failure_does_not_break_post_events`; `test_relay_subscriber_feeds_trusted_ingest` (повідомлення → `engine.ingest_detailed(..., trusted=True)` на резерві); `test_relay_ignores_garbage` (не-JSON, число, список із рядками).
- [ ] **Step 2:** FAIL. **Step 3:** `post_events` після `ingest_detailed` публікує `r.relay`, якщо `rt.ha` і лідер; потік підписки працює лише на резерві й DEGRADED не потрібен.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 9: Наскрізна перевірка перемикання

**Files:** Test `tests/test_ha_failover.py`.

- [ ] **Step 1: tests** (два `Engine` з `standby=True`, один спільний фейковий `LeaseStore`, один файл БД у `tmp_path`, ті самі події подаються в обох):
  - `test_leader_dies_standby_takes_over_without_new_alerts`: після підвищення `engine_b.tracker.confirmed()` має ті самі цілі, а `_evaluate` не піднімає повторної тривоги для вже підвищеного треку.
  - `test_alert_raised_during_gap_is_written_as_failover`: тривога піднята резервом до підвищення з'являється в `incidents` з `failover = 1`.
  - `test_old_leader_is_fenced_after_takeover`: старий двигун, що ожив з `epoch` 1, при записі отримує `FencedError` і стає неактивним; ланцюжок журналу дій після цього `verify()` OK.
  - `test_redis_restart_resets_epoch_but_cluster_recovers` (сховище скидає лічильник до 0, файл має 7: за кілька `step()` новий лідер отримує `epoch > 7`).
- [ ] **Step 2:** FAIL там, де чогось бракує; виправити в коді тих завдань. **Step 3:** PASS, повний `pytest -q`, `ruff`, `mypy`. **Step 4:** commit.

### Task 10: Розгортання й документація

**Files:** Modify `deploy/mosquitto.acl`, `deploy/mosquitto.passwd`, `docker-compose.yml`, `README.md`.

- [ ] **Step 1:** ACL: `user dronewatch-service-2` + `topic read dronewatch/events/#`. Пароль-хеш згенерувати `dronewatch-keygen` (демо-пароль, як у решти файлу, з тим самим застереженням у коментарі).
- [ ] **Step 2:** compose-профіль `ha`: сервіс `redis` (`redis:7`, без публікації портів), `service-2` (той самий образ, `MQTT_USER: dronewatch-service-2`, порт `127.0.0.1:8002`, **той самий том `incidents`**), а також `ha.enabled: true` для обох через окремий `deploy/config.ha.yaml`. Хост-порти пояснити в коментарі: балансувальник дивиться на `/readyz`.
- [ ] **Step 3:** `README.md`: розділ «Резерв (HA)»: що це, що це не горизонтальне масштабування, межа v1 (один вузол, спільний том), логін брокера на копію, `/readyz`, що робить кожна роль, як перевірити `kill`.
- [ ] **Step 4 (ручна перевірка, у звіті PR):** `docker compose --profile ha up`, відкрити `/readyz` на обох портах (один 200, другий 503), `docker kill` лідера, секундомір до 200 на другому (≤ ~8 с), перевірити `dronewatch-audit --verify` і що інциденти на місці.
- [ ] **Step 5:** commit, push, PR; перед злиттям чекати CI.

## Self-Review
- **Покриття spec:** оренда й запобіжник (T2, T3, T4), ролі (T6, T7), MQTT-логін (T10), релей (T5, T8), перемикання й `pending` (T4, T9), деградація (T2, T7), конфіг (T1), тести fail-closed старту (T6).
- **Узгодженість типів:** `FencedError` і `NotActiveError` визначаються в T2 (`ha.py`), `engine.py` імпортує звідти; `Role`, `Elector.is_valid()` — T2; `IngestResult.relay` — T5, використовується в T8.
- **Свідомі рішення поза spec:** DEGRADED лідер лишається «готовим» (`/readyz` 200), інакше при падінні Redis не лишилось би ніякої копії для людей; тривоги DEGRADED, втрачені на користь нового лідера, відкидаються з логом (він бачив ті самі події); розрив Redis, що скинув лічильник, лікується `raise_epoch`.
