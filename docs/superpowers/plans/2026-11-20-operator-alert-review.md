# Operator alert review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Оператор підтверджує/відхиляє тривоги; рішення (автор, час, коментар, історія) зберігається, видно в UI, `/api/incidents` і AI-тулах.

**Architecture:** Логіка рішень живе в `Engine` (він володіє SQLite-з'єднанням і замком), HTTP-ендпоінт тонкий. Стан рішення їде клієнтам у наявному `state`-повідомленні (`alerts[:30]` кожен скан), окремого WS-повідомлення не потрібно. Рішення нічого не пригнічує.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, pytest, ruff, mypy; фронтенд — `static/index.html` (ванільний JS).

**Spec:** `docs/superpowers/specs/2026-11-20-operator-alert-review-design.md`

## Global Constraints

- Статуси рівно: `new`, `confirmed`, `dismissed`. `NULL` у старих рядках `incidents` = `new`.
- Коментар ≤ 500 символів (валідація 422). Автор без входу = `anonymous`.
- Рішення не змінює `max_level`, трекер, зони, поведінку `_evaluate`.
- Ролей немає: достатньо `require_user`. AI-тули лишаються read-only.
- Перевірки перед кожним комітом: `python -m ruff check .`, `python -m mypy`, `python -m pytest -q` зелені.
- Коментарі й тексти UI — українською, як у решті коду.

## Review Focus

- Міграція БД, створеної до цієї зміни (без нових колонок/таблиці): сервіс стартує, старі рядки мають статус `new` у відповідях.
- `POST` для неіснуючого `id` → 404; `status` поза трійкою → 422; `note` > 500 → 422; порожній `note` нормалізується в `NULL`.
- Повторне рішення з тим самим статусом: не помилка, історія отримує новий запис.
- Рішення по тривозі, що вже випала з `engine.alerts` (200 останніх) — запис у БД працює, пам'ять не ламається.
- Коментар з HTML (`<img onerror=…>`) не виконується в UI (екранування `esc`).

---

### Task 1: Engine — зберігання й логіка рішень

**Files:**
- Modify: `src/dronewatch/service/engine.py` (TypedDict `Alert` ~50; `__init__` схема ~155-162; `_evaluate` ~378-408)
- Test: `tests/test_review.py` (створити)

**Interfaces:**
- Produces: `Alert` отримує `id: int`, `status: str`, `reviewed_by: str | None`, `reviewed_at: float | None`, `review_note: str | None`.
- Produces: `Engine.review_incident(self, incident_id: int, status: str, by: str, note: str | None = None) -> dict[str, Any] | None` — `None`, якщо `id` нема; інакше повертає оновлений рядок `incidents` як dict.
- Produces: `Engine.incident_stats(self, since_ts: float | None = None) -> list[dict[str, Any]]` — рядки `{"zone": str, "status": str, "count": int}`, `status` = `COALESCE(status,'new')`.
- Produces: `VALID_REVIEW_STATUSES: tuple[str, ...] = ("new", "confirmed", "dismissed")` у `engine.py`.

- [ ] **Step 1: Write failing tests** у `tests/test_review.py` (фікстура `eng` = `Engine(default_sensors(), [Zone("z", 100, "WARNING")], db_path=str(tmp_path/"i.db"))`; вставка тривоги — прямий `INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors)`):
  - `test_new_columns_and_table_exist_on_fresh_db`: `PRAGMA table_info(incidents)` містить `status, reviewed_by, reviewed_at, review_note`; існує таблиця `incident_reviews`.
  - `test_migrates_legacy_db`: створити файл БД зі старою схемою (без нових колонок, з одним рядком), відкрити `Engine` — не падає, `SELECT status` дає `NULL`, `incident_stats()` показує цей рядок як `new`.
  - `test_review_sets_fields_and_history`: `review_incident(1, "dismissed", "op", "дрон лелеки")` → `status=="dismissed"`, `reviewed_by=="op"`, `reviewed_at` > 0, `review_note=="дрон лелеки"`; потім `review_incident(1, "confirmed", "op2")` → у `incident_reviews` 2 рядки для `incident_id=1`, в `incidents` статус `confirmed`.
  - `test_review_unknown_id_returns_none`.
  - `test_empty_note_becomes_null`: `note="  "` → `review_note is None`.
  - `test_review_updates_alert_in_memory`: після `_evaluate`-тривоги (або вставки в `eng.alerts` з `id`) `eng.alerts[0]["status"]` змінюється; рішення по `id`, якого нема в `eng.alerts`, не кидає.
  - `test_alert_has_id_and_new_status`: тривога, створена `_evaluate` (тест-хелпер як у наявних тестах рушія), має `id` == rowid запису в БД і `status == "new"`.
  - `test_incident_stats_groups_by_zone_and_status`: три рядки (A new, A dismissed, B confirmed) → відповідні групи; `since_ts` відсікає старіші.
  - `test_review_does_not_touch_max_level`: `eng.max_level` до і після рішення однаковий.

- [ ] **Step 2:** `python -m pytest tests/test_review.py -q` → FAIL (немає методів/колонок).

- [ ] **Step 3: Implement.** У `__init__`: додати колонки міграцією (`ALTER TABLE … ADD COLUMN`, лише якщо їх нема в `PRAGMA table_info`): `status TEXT`, `reviewed_by TEXT`, `reviewed_at REAL`, `review_note TEXT`; `CREATE TABLE IF NOT EXISTS incident_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, status TEXT, by TEXT, ts REAL, note TEXT)`. `_evaluate`: брати `cur = self.db.execute(INSERT …)` і `cur.lastrowid` як `a["id"]`; тривога створюється зі `status="new"`, інші нові поля `None`; INSERT також пише `status='new'`. `review_incident`/`incident_stats`: під `self.lock`; `note` — `strip()`, порожнє → `None`, обрізання до 500; запис в історію + UPDATE `incidents` + `commit`; оновити відповідний елемент `self.alerts` (за `id`), якщо є. Час — `self.clock()`. SQL — лише параметризовані запити.

- [ ] **Step 4:** `python -m pytest tests/test_review.py -q` → PASS; потім повний `python -m pytest -q`, `ruff`, `mypy` (існуючі тести можуть порівнювати форму `Alert` — виправити очікування, не логіку).

- [ ] **Step 5: Commit** — `git add src/dronewatch/service/engine.py tests/test_review.py` / `git commit -m "incidents: operator review state, history and stats in Engine"`.

---

### Task 2: HTTP API

**Files:**
- Modify: `src/dronewatch/service/app.py` (`read_incidents` ~455, `/api/incidents` ~461, новий ендпоінт)
- Test: `tests/test_review_api.py` (створити; клієнт — як `_client` у `tests/test_ai_endpoint.py`, з `sessions.create("operator")`)

**Interfaces:**
- Consumes: `Engine.review_incident`, `VALID_REVIEW_STATUSES` (Task 1).
- Produces: `POST /api/incidents/{incident_id}/review`, тіло `ReviewRequest(status: Literal["new","confirmed","dismissed"], note: str | None = Field(default=None, max_length=500))`, відповідь 200 — оновлений рядок інциденту; 404 `{"detail": …}`; 401 без входу.
- Produces: `read_incidents(db_path: str, limit: int = 100, status: str | None = None)`; `status="new"` відповідає й `NULL`. `GET /api/incidents?status=` (422 на недопустиме значення).

- [ ] **Step 1: Write failing tests:**
  - `test_review_requires_login` → 401.
  - `test_review_ok_records_author_from_session`: після `POST …/1/review {"status":"confirmed","note":"бачив"}` → 200, `reviewed_by=="operator"`; `GET /api/incidents` показує `status=="confirmed"`.
  - `test_review_without_login_feature_uses_anonymous`: профіль без входу (як це робить `DashboardAccess` у демо — подивитися `tests/test_dashboard.py`) → `reviewed_by=="anonymous"`.
  - `test_review_404_unknown_id`, `test_review_422_bad_status`, `test_review_422_note_too_long` (501 символ).
  - `test_incidents_status_filter`: `?status=new` включає рядок із `NULL`; `?status=bogus` → 422.
  - `test_review_rejects_foreign_origin_and_non_json`: той самий захист, що в `json_post_guard` (зміна стану).

- [ ] **Step 2:** `python -m pytest tests/test_review_api.py -q` → FAIL.

- [ ] **Step 3: Implement** ендпоінт: залежності `require_user`-подібні, але автор береться через `current_user` (`None` → `"anonymous"` лише коли вхід вимкнено; коли вхід увімкнено, `require_user` дає 401 — повторити логіку, що вже є в `get_access(...)`/`DashboardAccess`, не вигадувати нову), плюс `Depends(json_post_guard)`. `read_incidents`: додати `WHERE COALESCE(status,'new') = ?` за наявності `status`. Рядок відповіді — з `read_incidents`/SELECT по `id`.

- [ ] **Step 4:** тести → PASS; повні `pytest`, `ruff`, `mypy`.

- [ ] **Step 5: Commit** — `git commit -m "api: POST /api/incidents/{id}/review and status filter"`.

---

### Task 3: AI-тул статистики

**Files:**
- Modify: `src/dronewatch/service/ai/tools.py` (`TOOL_SCHEMAS`, нова функція, `call_tool`)
- Test: `tests/test_ai_tools.py` (додати)

**Interfaces:**
- Consumes: `Engine.incident_stats` (Task 1). `get_incidents` вже повертає нові поля через `read_incidents`.
- Produces: `get_incident_stats(rt: Runtime, days: int = 7) -> list[dict[str, Any]]` і тул `get_incident_stats` з параметром `days` (integer, типово 7, clamp 1..365; нечислове значення не падає — як у виправленні для `limit` у `tests/test_ai_agent.py:68`).

- [ ] **Step 1: Write failing tests:** `test_get_incident_stats_counts_by_zone_and_status`; `test_get_incident_stats_bad_days_does_not_crash` (`{"days": "x"}` → дефолт); `test_tool_schemas_include_stats_and_stay_read_only` (є схема з name `get_incident_stats`, `call_tool` для нього нічого не пише в БД).
- [ ] **Step 2:** запуск → FAIL.
- [ ] **Step 3: Implement:** `since_ts = rt.engine.clock() - days*86400`; зареєструвати в `TOOL_SCHEMAS` і `call_tool`.
- [ ] **Step 4:** PASS; повні `pytest`, `ruff`, `mypy`.
- [ ] **Step 5: Commit** — `git commit -m "ai: get_incident_stats tool (confirmed/dismissed by zone)"`.

---

### Task 4: UI в index.html

**Files:**
- Modify: `src/dronewatch/service/static/index.html` (CSS біля `.alert` ~95-128; рендер тривог ~869-880; клік по `#alerts` ~954; заголовок `<h2 class="hd-alerts">` ~269)
- Test: `tests/test_static.py` (додати перевірки наявності розмітки/ендпоінта; логіку UI перевіряємо вручну — Playwright у середньому терміні)

**Interfaces:**
- Consumes: `msg.alerts[i]` з полями `id, status, reviewed_by, reviewed_at, review_note` (Task 1); `POST /api/incidents/{id}/review` (Task 2).

- [ ] **Step 1: Write failing static test:** `index.html` містить `/api/incidents/` та `review`, клас `.alert.reviewed` і елемент лічильника (наприклад `id="alerts-new"`).
- [ ] **Step 2:** запуск → FAIL.
- [ ] **Step 3: Implement:**
  - Картка тривоги: кнопки ✓ (`confirmed`) / ✕ (`dismissed`) з `data-id`; розкривне необов'язкове поле коментаря (Enter/кнопка відправляє); якщо статус уже виставлений — показується значок статусу, кнопки лишаються для зміни рішення.
  - `reviewed` (status ≠ `new`): `opacity .6` (разом із `.friend` не складається, береться мінімум), tooltip `«підтверджено/відхилено · автор · час · коментар»`, **усе, що від користувача (автор, коментар), через `esc`**.
  - Сортування: `new` зверху (стабільне, інакше за `ts` спадно). Лічильник у заголовку: кількість `new` серед `msg.alerts`.
  - Клік по кнопках не має фокусувати ціль (зупинити спливання/перевірити `e.target.closest("button")` у наявному обробнику ~954). `fetch(..., {method:"POST", headers:{"Content-Type":"application/json"}, credentials:"same-origin"})`; при помилці — toast з повідомленням; оновлення стану приходить зі наступного `state` (оптимістичний UI не потрібен). `alertsHtml`-кеш (`lastAlerts`) уже захищає від зайвих перебудов DOM — не ламати: розкрите поле коментаря не повинно скидатись між сканами (стан відкритої картки тримати в змінній за `id`).
  - Ключ `seenAlerts` лишається `ts:track_id:level` (рішення не повинно знову викликати toast).
- [ ] **Step 4:** `python -m pytest -q`; ручна перевірка: `docker compose --profile demo up` (або локальний запуск за README), у браузері: підтвердити, відхилити, змінити рішення, лічильник, коментар із `<b>x</b>` показується як текст.
- [ ] **Step 5: Commit** — `git commit -m "dashboard: confirm/dismiss alerts, reviewed state, unreviewed counter"`.

---

## Self-review

- Spec coverage: статуси/автор/час/коментар/історія → T1; API, фільтр, anonymous → T2; AI-агрегат → T3; UI (приглушення, сортування, лічильник, кнопки, коментар) → T4. «Нічого не пригнічує» → `test_review_does_not_touch_max_level`.
- WS: окреме повідомлення замінено полями в наявному `state` (`alerts[:30]` кожен скан) — відхилення від спеки в межах «на розсуд виконавця».
- Типи узгоджені: `review_incident`/`incident_stats`/`VALID_REVIEW_STATUSES` однакові в T1–T3.
- Оновлення документації: після T4 додати абзац у README (розділ про тривоги/API) — до коміту T4.
