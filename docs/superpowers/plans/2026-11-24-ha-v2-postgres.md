# HA v2 (PostgreSQL + Redis Sentinel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Два вузли (VPS-A, VPS-B) плюс свідок: автоматичне перемикання рушія, PostgreSQL (Patroni) і Redis (Sentinel) без split-brain.

**Architecture:** Тонкий адаптер `Db` над SQLite і PostgreSQL; оренда лідера й запобіжник epoch переїжджають у PostgreSQL (запобіжник атомарний із записом через `FOR SHARE`); Redis лишається лише для сесій, ліміту входів і релея; кластер описано в `deploy/ha-cluster/`. SQLite без HA лишається типовим і не змінюється.

**Tech Stack:** Python 3.12+, FastAPI, sqlite3, `psycopg[binary]` 3.x (опційно), redis-py Sentinel, PostgreSQL 16, Patroni, etcd, docker compose.

**Spec:** `docs/superpowers/specs/2026-11-24-ha-v2-postgres-design.md` (затверджено). Читати разом із `2026-11-23-ha-failover-design.md`.

## Global Constraints
- Перевірки перед кожним комітом: `python -m ruff check .`, `python -m mypy`, `python -m pytest -q` (зараз 580+ тестів, усі лишаються зеленими).
- Коментарі й докстринги українською, як у решті коду. Коміти українською, закінчувати рядком `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Типово `storage.backend: sqlite`; без `ha.enabled` новий код не виконується.
- DSN лише з `sslmode=verify-full` і `target_session_attrs=read-write`; пароль лише з `DRONEWATCH_PG_PASSWORD`, ніколи в DSN, логах чи тексті винятків.
- `storage.connect_timeout_s` = 2, `storage.statement_timeout_s` = 3. `ha.lease_ttl_s` типово 6 (2..60).
- Ролі PostgreSQL: `dw_owner` (DDL, міграція), `dw_app` (запити застосунку; без UPDATE/DELETE на `audit_log`).
- Redis у режимі Sentinel: лише TLS (`rediss`), інакше відмова при старті (політика `check_redis_url`).
- Залежність `psycopg` лише в extra `dronewatch[postgres]`; без неї `backend: postgres` не стартує (fail-closed). Тести PostgreSQL пропускаються без `DRONEWATCH_TEST_PG_DSN`; CI запускає postgres:16.
- Жодного тихого відкату на SQLite чи пам'ять, коли PostgreSQL налаштовано, але недоступний.

## Review Focus
1. Два одночасні `acquire` оренди: рівно один виграє (тест у Task 4).
2. Збій PostgreSQL посеред транзакції на спільному з'єднанні: відкіт, з'єднання не лишається в зламаному стані, перепідключення (Task 6).
3. Текст помилки драйвера з паролем чи DSN не потрапляє в лог, виняток, `/api` відповідь (Task 1, 6).
4. Розбіжність годинників між VPS: термін оренди рахується за годинником PostgreSQL (Task 4).
5. Дублі подій з двох брокерів не подвоюють треки й тривоги (Task 9).

---

## File Structure
- Create `src/dronewatch/service/store.py`: `Db`, `Store`, `SqliteStore`, `PostgresStore`, `StoreError`, `open_store`.
- Create `src/dronewatch/service/schema.py`: створення/міграція таблиць для обох діалектів (виносить `_open_db` з engine.py).
- Modify `engine.py`, `audit.py`, `replay.py`, `app.py`, `audit_cli.py`, `config.py`, `ha.py`, `dashboard_redis.py`.
- Create `src/dronewatch/service/pg_lease.py`: `PgLeaseStore`.
- Create `src/dronewatch/service/db_migrate.py`: `dronewatch-db-migrate`.
- Create `deploy/ha-cluster/` (compose, Patroni образ, шаблони, `gen-certs`), `scripts/ha_failover_drill.sh`.
- Tests: `tests/test_store.py`, `test_schema.py`, `test_audit_pg.py`, `test_pg_lease.py`, `test_db_failure.py`, `test_sentinel.py`, `test_storage_config.py`, `test_db_migrate.py`, `test_dual_broker.py`; фікстура `db_backend` у `tests/conftest.py`.

---

### Task 1: Адаптер сховища `Db`/`Store` і конфіг `storage`

**Files:**
- Create: `src/dronewatch/service/store.py`, `tests/test_store.py`
- Modify: `src/dronewatch/service/config.py` (клас `StorageCfg`, поле `Config.storage`), `tests/conftest.py`, `pyproject.toml` (extra `postgres`)

**Interfaces:**
- Produces:
  - `class StoreError(Exception)` (без тексту першоджерела).
  - `class Db(Protocol)`: `dialect: Literal["sqlite","postgres"]`; `execute(sql: str, params: Sequence[Any] = ()) -> Cursor` (SQL пишеться з `?`, для PostgreSQL перетворюється на `%s`); `insert_id(sql: str, params: Sequence[Any] = ()) -> int` (SQL без `RETURNING`); `columns(table: str) -> set[str]`; `commit() -> None`; `rollback() -> None`; `close() -> None`. `Cursor` має `fetchone`, `fetchall`, `description`.
  - `class Store(Protocol)`: `kind: Literal["sqlite","postgres"]`; `connect(*, readonly: bool = False) -> Db`.
  - `SqliteStore(path: str)`, `PostgresStore(dsn: str, password: str | None, connect_timeout_s: float, statement_timeout_s: float)`.
  - `open_store(cfg: StorageCfg, sqlite_path: str) -> Store`.
  - `StorageCfg`: `backend: Literal["sqlite","postgres"]="sqlite"`, `dsn: str | None`, `connect_timeout_s: float=2`, `statement_timeout_s: float=3`.
  - Фікстура `db_backend(request) -> Store` параметризована `sqlite` і `pg` (pg пропускається без `DRONEWATCH_TEST_PG_DSN`; кожен тест отримує чисту схему).

- [ ] **Step 1: Тести (`tests/test_store.py`).** `test_sqlite_insert_id_and_columns`: `insert_id` повертає зростаючі id, `columns("incidents")` після створення таблиці дає набір колонок. `test_placeholders_translated`: на PG запит із `?` виконується (параметризовано `db_backend`). `test_postgres_requires_psycopg` (monkeypatch імпорту): `PostgresStore(...).connect()` піднімає `StoreError("потрібен пакет psycopg: pip install 'dronewatch[postgres]'")`. `test_error_text_has_no_dsn_or_password`: невдале підключення до `postgresql://u@127.0.0.1:1/x?...` з паролем `s3cret`, у `str(exc)` і `repr(exc)` нема ні `s3cret`, ні адреси. `test_connect_timeouts_applied` (pg): `SHOW statement_timeout` дає 3000 ms.
- [ ] **Step 2: Запустити** `python -m pytest tests/test_store.py -q`. Очікування: FAIL (модуля нема).
- [ ] **Step 3: Реалізувати `store.py`** за інтерфейсом. PG: `psycopg.connect(dsn, password=..., connect_timeout=int(ceil(...)), options="-c statement_timeout=...")`, `autocommit=False`; помилки драйвера загортати в `StoreError(type(e).__name__)` з `from None`. `readonly=True` для SQLite: `mode=ro` URI; для PG: `SET TRANSACTION READ ONLY`. Додати `StorageCfg` і `Config.storage`; extra `postgres = ["psycopg[binary]>=3.2"]`.
- [ ] **Step 4: Запустити** `python -m pytest tests/test_store.py -q`. Очікування: PASS (pg-варіанти SKIPPED без DSN; локально підняти `docker run -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16` і задати `DRONEWATCH_TEST_PG_DSN`, перевірити pg-варіанти теж).
- [ ] **Step 5: Коміт** `feat(store): адаптер Db/Store для SQLite і PostgreSQL`.

### Task 2: Схема й рушій працюють через `Db` (лише SQLite, без зміни поведінки)

**Files:**
- Create: `src/dronewatch/service/schema.py`, `tests/test_schema.py`
- Modify: `src/dronewatch/service/engine.py` (`_open_db`, `activate`, `_deactivate_locked`, `_insert_incident`, `_flush_pending`, `review_incident`, анотації типів), `audit.py` (лише тип параметра `db: Db`), `replay.py` (`ReplayRecorder(db: Db, ...)`)

**Interfaces:**
- Consumes: `Db`, `Store`, `SqliteStore` (Task 1).
- Produces: `ensure_schema(db: Db) -> None` (створює й мігрує `incidents`, `incident_reviews`, `incident_replays`, `ha_epoch`; ідемпотентна); `Engine.activate(target: str | Store, epoch: int) -> None` (рядок означає `SqliteStore(path)`); `Engine.__init__` приймає `db_path: str` як раніше.

- [ ] **Step 1: Тести (`tests/test_schema.py`).** `test_ensure_schema_idempotent` (двічі підряд без помилки, колонки `model, model_name, side, status, reviewed_by, reviewed_at, review_note, failover` є); `test_ensure_schema_migrates_old_incidents` (стара таблиця без нових колонок добудовується, старі рядки лишаються з NULL). Наявні `tests/test_ha_engine.py`, `test_review.py`, `test_replay.py` лишаються без змін і мають пройти.
- [ ] **Step 2: Запустити** `python -m pytest tests/test_schema.py -q`. Очікування: FAIL.
- [ ] **Step 3: Перенести створення таблиць** з `_open_db` у `schema.ensure_schema(db)`; `_open_db(path)` стає тонкою обгорткою `SqliteStore(path).connect()` плюс `ensure_schema`. Замінити `cur.lastrowid` на `db.insert_id(...)`, `PRAGMA table_info` на `db.columns(...)`, `sqlite3.connect` у `engine.py`/`replay.py` на `Db`. `BEGIN IMMEDIATE` у `activate` лишається поки в гілці `dialect == "sqlite"`. Не змінювати SQL-семантику.
- [ ] **Step 4: Повний прогін** `python -m pytest -q`, `ruff`, `mypy`. Очікування: усе зелене.
- [ ] **Step 5: Коміт** `refactor(engine): схема й запити через Db (SQLite, без зміни поведінки)`.

### Task 3: Схема PostgreSQL і журнал дій з `audit_head`

**Files:**
- Modify: `schema.py` (гілка `postgres`), `audit.py` (`AuditLog`: `append_locked`, `verify`, створення таблиць/тригерів за діалектом), `tests/test_audit.py` (параметризувати `db_backend`)
- Test: `tests/test_audit_pg.py`

**Interfaces:**
- Consumes: `Db.dialect`, `ensure_schema`.
- Produces: на PG таблиці `audit_log(id BIGINT PRIMARY KEY, ts DOUBLE PRECISION, ..., prev_hash, hash)` (id призначає код, не IDENTITY), `audit_head(id=1 CHECK, last_id BIGINT, head_hash TEXT)`; тригер `audit_log_immutable` (UPDATE/DELETE → `RAISE EXCEPTION 'audit_log is append-only'`); `incidents.id` і `incident_reviews.id` як `BIGINT GENERATED ALWAYS AS IDENTITY`; `incident_replays.data BYTEA`. `AuditLog.verify()` на PG додатково порівнює `audit_head.last_id/head_hash` з хвостом таблиці. Інтерфейс `AuditLog` не змінюється.

- [ ] **Step 1: Тести.** Існуючі тести `test_audit.py` пройти на обох бекендах. У `test_audit_pg.py` (pg): `test_append_assigns_gapless_ids_after_rollback` (відкат вставки не лишає діру: наступний id = попередній+1); `test_update_and_delete_rejected` (UPDATE/DELETE дають помилку); `test_truncated_tail_detected` (видалити останній рядок через `dw_owner`-з'єднання в обхід тригера й `audit_head` лишити: `verify().ok is False`, `broken_at` = id видаленого); `test_concurrent_appends_keep_chain` (8 потоків × 25 записів: `verify().ok`, `rows == 200`); `test_app_role_cannot_modify` (з'єднання під `dw_app`: `UPDATE audit_log` відмовлено правами; роль створити у фікстурі тесту).
- [ ] **Step 2: Запустити**, очікування: FAIL (pg-гілки нема).
- [ ] **Step 3: Реалізувати.** `append_locked` на PG: `SELECT last_id, head_hash FROM audit_head WHERE id=1 FOR UPDATE` (створити рядок `0, GENESIS` при ініціалізації), `id = last_id+1`, вставка, `UPDATE audit_head`. `verify()` на PG читає без `sqlite_sequence`. Додати SQL-файл ролей `deploy/ha-cluster/postgres-roles.sql` (створення `dw_owner`, `dw_app`, права) і викликати схему лише під `dw_owner`; застосунок працює під `dw_app`.
- [ ] **Step 4: Запустити** `python -m pytest tests/test_audit.py tests/test_audit_pg.py -q` проти локального PG. Очікування: PASS.
- [ ] **Step 5: Коміт** `feat(audit): журнал дій у PostgreSQL з audit_head і незмінністю`.

### Task 4: Оренда лідера й атомарний запобіжник у PostgreSQL

**Files:**
- Create: `src/dronewatch/service/pg_lease.py`, `tests/test_pg_lease.py`
- Modify: `engine.py` (`activate`, `_fence_locked`, `fence`), `schema.py` (таблиця `ha_lease`)

**Interfaces:**
- Consumes: `LeaseStore` Protocol з `ha.py` (`acquire(owner, ttl_ms) -> int | None`, `renew(owner, epoch, ttl_ms) -> bool`, `release(owner, epoch) -> None`, `raise_epoch(at_least) -> None`), `Store`.
- Produces: `PgLeaseStore(store: Store)`; таблиця `ha_lease(id=1 CHECK, owner TEXT, epoch BIGINT, expires_at TIMESTAMPTZ)`. Помилки як `LeaseStoreError(type(e).__name__)`.

- [ ] **Step 1: Тести (`test_pg_lease.py`, pg).** `test_acquire_gives_epoch_above_ha_epoch` (після запису `ha_epoch=7` перший acquire дає 8). `test_second_acquire_while_valid_returns_none`. `test_two_concurrent_acquires_one_wins` (два потоки з бар'єром, 20 раундів: у кожному рівно один не-None). `test_expiry_uses_db_clock` (ttl 300 ms, `time.sleep(0.5)`: другий власник бере оренду; годинник Python тут підмінити не можна й не потрібен). `test_renew_wrong_owner_false`, `test_release_then_acquire_increments_epoch`. `test_old_leader_fenced_on_write`: A бере epoch 1, активується, B після закінчення бере epoch 2 й активується, A пробує `review_incident(...)` і отримує `FencedError`, ніяких рядків від A у БД. `test_new_leader_waits_for_inflight_write` (транзакція A тримає `FOR SHARE`: `activate` B блокується до commit A, не ламаючи узгодженості).
- [ ] **Step 2: Запустити**, очікування: FAIL.
- [ ] **Step 3: Реалізувати `PgLeaseStore`** транзакціями з `SELECT ... FOR UPDATE` за спекою. `epoch = max(lease.epoch, ha_epoch.epoch) + 1`. Закінчення оренди порівнювати `expires_at < clock_timestamp()` у SQL. `Engine._fence_locked` на PG: `SELECT epoch FROM ha_epoch WHERE id=1 FOR SHARE` у тій самій транзакції, що й запис (commit існуючими викликами); публічний `fence()` робить `rollback()` після перевірки, щоб не тримати блокування. `activate` на PG: `UPDATE ha_epoch SET epoch=%s WHERE epoch <= %s` (0 рядків: читаємо збережений і піднімаємо `FencedError(stored=...)`); перший запуск вставляє рядок через `INSERT ... ON CONFLICT DO NOTHING`.
- [ ] **Step 4: Запустити** `python -m pytest tests/test_pg_lease.py tests/test_ha_lease.py tests/test_ha_engine.py tests/test_ha_failover.py -q`. Очікування: PASS.
- [ ] **Step 5: Коміт** `feat(ha): оренда лідера й запобіжник epoch у PostgreSQL`.

### Task 5: Читання з API й replay через `Store`, конфіг і проводка `app.py`

**Files:**
- Modify: `app.py` (`Runtime.store`, `build_runtime`, `build_ha`, `read_incidents`, `incident_replay`), `replay.py` (`read_replay(store, incident_id)`), `config.py` (валідація), `audit_cli.py`, `deploy/config.ha.yaml`
- Test: `tests/test_storage_config.py`; оновити `test_replay.py`, `test_ha_app.py`

**Interfaces:**
- Consumes: `open_store`, `PgLeaseStore`, `Engine.activate(Store, epoch)`.
- Produces: `Runtime.store: Store`; `read_incidents(store: Store, limit, status)`; `read_replay(store: Store, incident_id)`; `HaCfg.lease_store: Literal["redis","postgres"]="redis"`.

- [ ] **Step 1: Тести (`test_storage_config.py`).** `test_postgres_requires_ha` (`storage.backend: postgres` без `ha.enabled` відмовлено). `test_postgres_requires_verify_full` (DSN без `sslmode=verify-full` або без `target_session_attrs=read-write`: `ValueError`). `test_dsn_must_not_contain_password` (`postgresql://u:pw@h/db` відмовлено). `test_lease_store_postgres_needs_postgres_backend`. `test_missing_psycopg_fails_startup` (старт сервісу з `backend: postgres` без пакета: сервіс не піднімається, повідомлення про extra). `test_sqlite_default_unchanged` (типовий конфіг працює як раніше). Наявні HA-тести (`test_ha_app.py`) лишаються зеленими.
- [ ] **Step 2: Запустити**, очікування: FAIL.
- [ ] **Step 3: Реалізувати.** `build_ha`: за `cfg.ha.lease_store` вибирає `PgLeaseStore(rt.store)` або `RedisLeaseStore`; `promote(epoch)` викликає `engine.activate(rt.store, epoch)`. `dronewatch-audit --verify` отримує `--dsn` (пароль з `DRONEWATCH_PG_PASSWORD`). Нічого не відкривати `sqlite3.connect(db_path)` напряму в `app.py`/`replay.py`: усе через `Store.connect(readonly=True)`.
- [ ] **Step 4: Повний прогін** (`pytest -q`, `ruff`, `mypy`) + проти PG: `DRONEWATCH_TEST_PG_DSN=... pytest tests/test_ha_app.py`. Очікування: зелене.
- [ ] **Step 5: Коміт** `feat(app): storage.backend postgres і проводка оренди`.

### Task 6: Збій PostgreSQL під час роботи (pending, вимикач, перепідключення)

**Files:**
- Modify: `engine.py` (`_db_failed`, `_ensure_db`, константи `DB_BREAKER_S = 5.0`), `store.py` (виявлення зламаного з'єднання)
- Test: `tests/test_db_failure.py`

**Interfaces:**
- Consumes: `StoreError`, `Engine._stash_pending`, `_flush_pending`.
- Produces: `Engine.db_healthy -> bool` (для `/readyz` і `/api/health`: `storage.ok`, `storage.since`).

- [ ] **Step 1: Тести** (фейковий `Db`, що кидає `StoreError` за командою; без реального PG). `test_alert_goes_to_pending_when_db_fails` (тривога на екрані, `pending` не порожній, рядка нема). `test_breaker_skips_db_after_failure` (протягом 5 с інші скани не торкаються БД; лічильник викликів фейка не росте; замок `engine.lock` не тримається довше за один скан). `test_reconnect_after_breaker` (після 5 с `store.connect()` викликається, `pending` дописується без дублів). `test_review_during_outage_returns_503_not_silent` (`review_incident` піднімає `NotActiveError`-сумісну помилку, API дає 503, памʼять не змінюється). `test_failed_transaction_rolled_back` (після збою на половині `review_incident` наступний запит не бачить напівтранзакції). `test_log_has_no_secret` (caplog без пароля й DSN).
- [ ] **Step 2: Запустити**, очікування: FAIL.
- [ ] **Step 3: Реалізувати.** Усі місця запису в `engine.py` обгорнути так: `StoreError` → `rollback()`, закрити з'єднання, поставити `db_down_until = clock()+DB_BREAKER_S`, тривоги в `_stash_pending`. Поки `clock() < db_down_until`, БД не чіпати. `_ensure_db()` перепідключає через `self.store.connect()` і викликає `_flush_pending`. `health` отримує `storage`.
- [ ] **Step 4: Запустити** `python -m pytest tests/test_db_failure.py tests/test_ha_failover.py -q`. Очікування: PASS.
- [ ] **Step 5: Коміт** `feat(ha): збій PostgreSQL не зупиняє сканування, тривоги в pending`.

### Task 7: Redis Sentinel

**Files:**
- Modify: `dashboard_redis.py` (`connect_sentinel`), `config.py` (`RedisSentinelCfg`: `sentinels: list[str]`, `master_name: str`), `app.py` (`build_ha`/сесії обирають клієнта), `dashboard_auth.py` (`check_redis_url` для Sentinel: лише TLS)
- Test: `tests/test_sentinel.py`

**Interfaces:**
- Produces: `connect_sentinel(sentinels: Sequence[tuple[str,int]], master_name: str, password: str | None, ca: str | None) -> redis.Redis` (клієнт із тими самими таймаутами й пінгом, що `connect`; помилка: `SessionStoreError` без тексту першоджерела).

- [ ] **Step 1: Тести.** `test_sentinel_without_tls_rejected` (конфіг відмовлено). `test_connect_sentinel_failure_is_session_store_error_without_password`. `test_failover_reconnects` (з фейковим Sentinel-клієнтом: після зміни master наступна команда йде на новий; `RedisEventRelay.subscribe` відновлює підписку: існуючий цикл перестворює `ps`, перевірити тестом на фейку, що кидає `ConnectionError` один раз). Наявні `test_redis_sessions.py`, `test_ha_lease.py` зелені.
- [ ] **Step 2: Запустити**, очікування: FAIL.
- [ ] **Step 3: Реалізувати** `connect_sentinel` через `redis.sentinel.Sentinel(...).master_for(master_name, ...)`; пароль із `DRONEWATCH_REDIS_PASSWORD`.
- [ ] **Step 4: Запустити** `python -m pytest tests/test_sentinel.py tests/test_redis_sessions.py -q`. Очікування: PASS.
- [ ] **Step 5: Коміт** `feat(redis): Sentinel для сесій і релея`.

### Task 8: Міграція SQLite в PostgreSQL

**Files:**
- Create: `src/dronewatch/service/db_migrate.py`, `tests/test_db_migrate.py`
- Modify: `pyproject.toml` (`dronewatch-db-migrate = "dronewatch.service.db_migrate:main"`)

**Interfaces:**
- Consumes: `open_store`, `ensure_schema`, `AuditLog.verify`.
- Produces: `main(argv: list[str] | None = None) -> int` (0 успіх, 1 перевірка не зійшлась, 2 помилка/відмова); прапорці `--from PATH`, `--dsn DSN`.

- [ ] **Step 1: Тести (pg).** `test_round_trip_preserves_rows_ids_hashes` (джерело: інциденти, рішення, replay, журнал дій із SQLite-тестових фікстур; після міграції `audit_log.hash/prev_hash` побайтно рівні, `verify().ok`). `test_sequences_continue_after_max` (наступний інцидент отримує max+1; наступний запис журналу max+1). `test_refuses_non_empty_target` (код 2, нічого не змінено). `test_source_unchanged` (хеш файлу той самий). `test_ha_epoch_not_below_source`. `test_broken_source_chain_aborts` (пошкоджене джерело: код 1, ціль чиста: усе в одній транзакції).
- [ ] **Step 2: Запустити**, очікування: FAIL.
- [ ] **Step 3: Реалізувати** одну транзакцію на всю міграцію, `INSERT` з явними id (для `IDENTITY` `OVERRIDING SYSTEM VALUE`), потім `setval` послідовностей і `audit_head`.
- [ ] **Step 4: Запустити** `python -m pytest tests/test_db_migrate.py -q`. Очікування: PASS.
- [ ] **Step 5: Коміт** `feat(migrate): dronewatch-db-migrate з SQLite у PostgreSQL`.

### Task 9: Два брокери: ідемпотентність дублів

**Files:**
- Test: `tests/test_dual_broker.py`
- Modify: за потреби `engine.py` (лише якщо тест виявить подвоєння); `README.md` (приклад датчика з двома брокерами)

**Interfaces:**
- Consumes: `Engine.ingest_detailed`.

- [ ] **Step 1: Тест.** `test_duplicate_events_do_not_double_tracks_or_alerts`: ті самі 50 подій (однакові `sensor_id`, `ts`, вимір) подаються двічі (імітація двох брокерів); кількість треків і тривог така сама, як для одного разу; `stats["events_in"]` може рости, але `tracks`/`incidents` ні. `test_standby_and_leader_see_same_state_with_dup`: лідер і резерв отримують подвоєний потік, після підвищення резерву нових дублів тривог нема.
- [ ] **Step 2: Запустити.** Якщо PASS одразу, дубль уже безпечний: лишити як регресійний. Якщо FAIL: додати дедуплікацію за `(sensor_id, ts, значення)` з обмеженим вікном у `Engine.ingest_detailed` і запустити знову.
- [ ] **Step 3: Документація** приклад датчика з двома брокерами в README (розділ HA).
- [ ] **Step 4: Коміт** `test(ha): дублі подій із двох брокерів безпечні`.

### Task 10: Розгортання кластера

**Files:**
- Create: `deploy/ha-cluster/node-a/docker-compose.yml`, `node-b/docker-compose.yml`, `witness/docker-compose.yml`, `patroni/Dockerfile`, `patroni/patroni.yml.tmpl`, `etcd/`, `sentinel/sentinel.conf.tmpl`, `redis/redis.conf.tmpl`, `postgres-roles.sql` (з Task 3), `gen-certs.sh`, `env.example`, `README.md`
- Modify: `.github/workflows/ci.yml` (job `postgres`: сервіс postgres:16, `pip install -e ".[dev,postgres]"`, `pytest` з `DRONEWATCH_TEST_PG_DSN`; job `ha-cluster-lint`: `docker compose -f ... config -q` для трьох файлів і `shellcheck`)

**Interfaces:** немає коду; вихід це файли, що проходять `docker compose config`.

- [ ] **Step 1: Перевірка-«тест».** Додати `scripts/check_ha_cluster.sh`: для кожного compose виконує `docker compose --env-file env.example config -q`; перевіряє grep-ом, що жоден сервіс не публікує порт на `0.0.0.0` (лише `${WG_IP}:порт`), що в Patroni-шаблоні `synchronous_mode: true` і `synchronous_mode_strict: false`, що `ssl: on` і `sslmode`-вимоги є, що Sentinel/Redis мають `tls-port` і `requirepass`. Запустити: FAIL (файлів нема).
- [ ] **Step 2: Створити файли.** Образ Patroni: `postgres:16` + `pip install patroni[etcd3]`; etcd 3 члени (A, B, witness) із peer- і client-TLS; Sentinel quorum 2, 3 екземпляри; Redis master/replica на A/B із `requirepass`+TLS; PostgreSQL `pg_hba` лише `hostssl` із `scram-sha-256`. Сервіси `ha-a` і `ha-b` (образ сервісу) з `storage.backend: postgres`, `ha.lease_store: postgres`, MQTT-брокер на кожному вузлі. `gen-certs.sh` розширює наявний `deploy/gen-certs.sh` (СА один, сертифікати по ролях, SAN за адресами WireGuard). README: схема трьох машин, приклад WireGuard mesh і правил фаєрвола, порядок запуску (witness, A, B), створення ролей, `dronewatch-db-migrate`, нагадування, що репліка не резервна копія (pgBackRest окремо).
- [ ] **Step 3: Запустити** `bash scripts/check_ha_cluster.sh`. Очікування: PASS.
- [ ] **Step 4: Коміт** `feat(deploy): кластер із двох вузлів і свідка (Patroni, etcd, Sentinel)`.

### Task 11: Репетиція перемикання

**Files:**
- Create: `deploy/ha-cluster/sim/docker-compose.yml` (профіль `ha-cluster-sim`: усі ролі на одній машині в окремих мережах), `scripts/ha_failover_drill.sh`
- Modify: `deploy/ha-cluster/README.md` (розділ «Репетиція»)

**Interfaces:**
- Produces: `ha_failover_drill.sh <scenario>` зі сценаріями `kill-leader`, `kill-pg-master`, `kill-redis-master`, `isolate-node-a`, `kill-witness`. Для кожного: вбиває контейнер(и), чекає відновлення (`/readyz` лідера 200 і успішний запис тестового інциденту), друкує секунди до відновлення, перевіряє `dronewatch-audit --verify --dsn` (OK) і що в `incidents` нема дублів за `(zone, level, ts±window)`; код виходу 0 лише якщо відновлення вклалось у `lease_ttl_s * 4 + 30` с і split-brain не було (`ha_epoch` не двічі той самий з різними власниками).

- [ ] **Step 1: Написати скрипт і sim-compose**, запустити без підготовленого кластера: очікувана відмова з зрозумілим повідомленням.
- [ ] **Step 2: Підняти sim** (`docker compose -f deploy/ha-cluster/sim/docker-compose.yml up -d --build`) і прогнати всі п'ять сценаріїв. Очікування: усі завершуються кодом 0; записати виміряні секунди в README.
- [ ] **Step 3: Окремо вручну** відрізати VPS-A від мережі (iptables/ `docker network disconnect`) за наявності лідера на A: перевірити, що A стає DEGRADED/резервом, B лідером, двох писачів нема. Зафіксувати результат у README. Якщо сценарій виявить подвійний запис: зупинитись і повернутись до Task 4.
- [ ] **Step 4: Коміт** `feat(deploy): репетиція перемикання (sim-профіль і скрипт)`.

---

## Self-review
- Покриття спеки: адаптер (T1,2), PG схема й журнал (T3), оренда/запобіжник (T4), конфіг/проводка (T5), збій БД (T6), Sentinel (T7), міграція (T8), два брокери (T9), розгортання/WireGuard/TLS/ролі (T3,T10), репетиція (T11), CI з postgres:16 (T10). Питання 1 (два брокери) у T9/T10, питання 2 (без strict) у T10.
- Узгодженість типів: `Store.connect(readonly)`, `Db.insert_id`, `Engine.activate(target: str | Store, epoch)`, `PgLeaseStore(store)` однакові в усіх задачах.
- Розбіжність зі спекою, яку виконавець має знати: `activate` повертає `FencedError` через `UPDATE ... WHERE epoch <= %s`, а не `BEGIN IMMEDIATE` (T4); тип id таблиць `incidents` на PG `IDENTITY` (T3).
