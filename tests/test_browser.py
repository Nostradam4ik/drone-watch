"""Дашборд у справжньому браузері (Playwright, Chromium). Запуск: pytest -m browser

Сервіс піднімається окремим процесом у demo-режимі з трьома користувачами (viewer, operator, admin): вхід обовʼязковий,
відкритий HTTP лише на 127.0.0.1. Сторінку, статику
й HTTP API віддає він сам. WebSocket у більшості тестів підмінено: тест сам шле знімок стану, тож тривоги й треки
видно одразу і щоразу ті самі (симуляція демо йде в реальному часі, перші тривоги лише за ~40-60 с). Один тест іде
на живому WebSocket, щоб підміна не приховала розбіжність із сервером.

Кожен тест падає на будь-якій JS-помилці сторінки (pageerror, console.error): так розбиття index.html на файли
не зможе непомітно зламати, скажімо, функцію, яка опинилася не в тому файлі.

Без Playwright тести пропускаються. DW_REQUIRE_BROWSER=1 (CI) перетворює пропуск на збій."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "browser-test-password-1"
ROLES = ("viewer", "operator", "admin")
pytestmark = pytest.mark.browser

if os.environ.get("DW_REQUIRE_BROWSER") == "1":
    from playwright.sync_api import Page, WebSocketRoute, expect, sync_playwright
else:
    sync_api = pytest.importorskip("playwright.sync_api", reason="pip install -e '.[browser]' && playwright install chromium")
    Page, WebSocketRoute, expect, sync_playwright = sync_api.Page, sync_api.WebSocketRoute, sync_api.expect, sync_api.sync_playwright


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.load(r)


def login_json(base: str, role: str, path: str) -> Any:
    """Входить через /api/login і віддає JSON за `path` (конфіг потрібен тестам до відкриття сторінки)."""
    req = urllib.request.Request(base + "/api/login", data=json.dumps({"username": role, "password": PASSWORD}).encode(),
                                 headers={"Content-Type": "application/json", "Origin": base})
    with urllib.request.urlopen(req, timeout=5) as r:
        cookie = r.headers["Set-Cookie"].split(";")[0]
    with urllib.request.urlopen(urllib.request.Request(base + path, headers={"Cookie": cookie}), timeout=5) as r:
        return json.load(r)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Сервіс у demo-режимі. Робоча тека тимчасова: власної карти там немає (map: none), журнал інцидентів свій."""
    work = tmp_path_factory.mktemp("browser")
    port = free_port()
    from dronewatch.service.dashboard_auth import hash_password
    h = hash_password(PASSWORD)
    conf = work / "config.yaml"
    conf.write_text((ROOT / "deploy" / "config.demo.yaml").read_text(encoding="utf-8") + "dashboard:\n  users:\n" +
                    "".join(f'    - {{name: {r}, password_hash: "{h}", role: {r}}}\n' for r in ROLES), encoding="utf-8")
    env = {**os.environ, "CONFIG": str(conf), "DB_PATH": str(work / "incidents.db")}
    env.pop("MISTRAL_API_KEY", None)
    log = (work / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "dronewatch.service.app:app", "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                get_json(base + "/api/health")   # публічний: без входу віддає лише {"ok": ...}
                break
            except OSError:
                if proc.poll() is not None or time.time() > deadline:
                    log.close()
                    raise RuntimeError("сервіс не стартував:\n" + (work / "server.log").read_text(encoding="utf-8")) from None
                time.sleep(0.2)
        yield {"base": base, "cfg": login_json(base, "admin", "/api/config")}
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:   # пакет є, а браузер не скачано: локально пропуск, у CI збій
            if os.environ.get("DW_REQUIRE_BROWSER") == "1":
                raise
            pytest.skip(f"Chromium для Playwright не встановлено (playwright install chromium): {str(e).splitlines()[0]}")
        yield b
        b.close()


class Dashboard:
    """Відкрита сторінка дашборда з підміненим WebSocket. push() шле кадр стану так, ніби його надіслав сервер."""

    def __init__(self, page: Page, cfg: dict[str, Any]) -> None:
        self.page, self.cfg = page, cfg
        self.ws: WebSocketRoute | None = None
        self.errors: list[str] = []

    def push(self, msg: dict[str, Any]) -> None:
        assert self.ws is not None, "сторінка ще не відкрила WebSocket"
        self.ws.send(json.dumps(msg))

    def state(self, tracks: list[dict[str, Any]] | None = None, alerts: list[dict[str, Any]] | None = None,
              sensors: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        if sensors is None:
            sensors = [{"id": s["id"], "label": s["kind"], "state": "OK", "age_s": 0.0} for s in self.cfg["sensors"]]
        stats = {"events_in": 1234, "events_rejected": 0, "events_unauthorized": 0, "events_dropped": 0, "heartbeats": 10,
                 "scans": 50, "scan_errors": 0, "tracks_capped": 0, "bearings_truncated": 0}
        return {"type": "state", "t": 12.0, "tracks": tracks or [], "stats": stats,
                "calibration": {"done": True, "note": "тест"}, "sensors": sensors, "layout_rev": self.cfg.get("layout_rev", 0),
                "tentative": 1, "alerts": alerts or [], "mqtt": None, **extra}


def track(tid: int, level: str = "NONE", side: str = "unknown", **kw: Any) -> dict[str, Any]:
    t = {"id": tid, "x": 100.0 + tid, "y": 50.0, "z": 60.0, "vx": -10.0, "vy": 1.0, "vz": 0.0, "speed": 10.1, "err": 8.0,
         "score": 3.0, "kinds": ["radar", "rf"], "coasting": False, "level": level, "diving": False, "ttg": 20.0,
         "model": "dji-mini-4", "model_name": "DJI Mini 4 Pro", "side": side, "country": "КНР", "drone_kind": "квадрокоптер",
         "shape": "quad", "identified_by": "rf", "identity_conflict": False, "alt_known": True}
    t.update(kw)
    return t


def alert(aid: int, track_id: int, level: str = "CRITICAL", status: str = "new", **kw: Any) -> dict[str, Any]:
    a = {"id": aid, "ts": 1795352309.0 + aid, "track_id": track_id, "level": level, "zone": "Критична зона radar1",
         "pos": [100.0, 50.0, 60.0], "speed": 14.9, "sensors": ["radar", "rf"], "model": "dji-mini-4",
         "model_name": "DJI Mini 4 Pro", "side": "unknown", "status": status, "reviewed_by": None, "reviewed_at": None,
         "review_note": None}
    a.update(kw)
    return a


def watch_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # «Failed to load resource» браузер пише сам на кожну HTTP-помилку, зокрема навмисну в тесті. Файли, що не
    # завантажилися, ловить test_static_app_files_load_without_404.
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
    return errors


def sign_in(page: Page, base: str, role: str, username: str = "", password: str = PASSWORD) -> int:
    """Вхід у контексті браузера (кука лишається в ньому). Повертає HTTP-статус."""
    page.goto(base + "/login")
    return int(page.evaluate("""async ([u, p]) => (await fetch("/api/login", {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({username: u, password: p})})).status""",
                             [username or role, password]))


@pytest.fixture
def dash_as(server: dict[str, Any], browser: Any) -> Iterator[Any]:
    """dash_as(role) відкриває дашборд користувачем із цією роллю, WebSocket підмінено (кадри шле тест)."""
    ctxs: list[Any] = []

    def make(role: str) -> Dashboard:
        ctx = browser.new_context(reduced_motion="reduce", viewport={"width": 1400, "height": 900})
        ctxs.append(ctx)
        page = ctx.new_page()
        errors = watch_errors(page)
        assert sign_in(page, server["base"], role) == 200
        d = Dashboard(page, server["cfg"])
        d.errors = errors

        def on_ws(ws: WebSocketRoute) -> None:   # до сервера не підключаємось: кадри шле тест
            d.ws = ws
        page.route_web_socket("**/ws", on_ws)
        page.goto(server["base"] + "/")
        expect(page.locator("#conn")).to_have_text("на звʼязку")
        return d
    yield make
    for c in ctxs:
        c.close()


@pytest.fixture
def dash(dash_as: Any) -> Iterator[Dashboard]:
    """Оператор: вирішує тривоги й питає AI (наявні тести працюють саме так)."""
    d = dash_as("operator")
    yield d
    assert not d.errors, "\n".join(d.errors)


# ---------- живий сервер ----------
def test_page_loads_and_shows_live_tracks_from_the_real_websocket(server, browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    errors = watch_errors(page)
    assert sign_in(page, server["base"], "admin") == 200
    page.goto(server["base"] + "/")
    expect(page.locator("#conn")).to_have_text("на звʼязку")
    expect(page.locator("#mode")).to_have_text("ДЕМО (симуляція)")
    expect(page.locator("#tbody tr").first).to_be_visible(timeout=20_000)   # симуляція: перший трек за кілька секунд
    expect(page.locator("#sensors .sensor").first).to_be_visible()
    expect(page.locator("#edit-sensors")).to_be_visible()   # демо: розстановка датчиків доступна
    ctx.close()
    assert not errors, "\n".join(errors)


def test_sensor_height_is_edited_and_saved_with_the_layout(server, browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    errors = watch_errors(page)
    assert sign_in(page, server["base"], "admin") == 200
    page.goto(server["base"] + "/")
    expect(page.locator("#sensors .sensor").first).to_be_visible(timeout=20_000)
    page.locator("#edit-sensors").click()
    field = page.locator("#sensors input[data-h]").first
    expect(field).to_be_visible()
    sid = field.get_attribute("data-h")
    with page.expect_request(lambda r: r.url.endswith("/api/layout") and r.method == "PUT") as req:
        field.fill("15")
        field.press("Enter")
    body = json.loads(req.value.post_data or "{}")
    assert {s["id"]: s["height"] for s in body["sensors"]}[sid] == 15, "висота йде в тілі PUT"
    assert all("height" in s for s in body["sensors"])
    expect(page.locator(f'#sensors input[data-h="{sid}"]')).to_have_value("15")
    # ще раз збереження іншим способом не скидає висоту (регрес: редактор раніше не надсилав height взагалі)
    page.locator("#edit-sensors").click()
    assert {s["id"]: s["height"] for s in login_json(server["base"], "admin", "/api/config")["sensors"]}[sid] == 15
    page.locator("#edit-sensors").click()
    with page.expect_request(lambda r: r.url.endswith("/api/layout") and r.method == "DELETE"):
        page.locator("#layout-reset").click()   # повертаємо вбудовані датчики: решта тестів модуля чекає демо-розстановку
    ctx.close()
    assert not errors, "\n".join(errors)


def test_static_app_files_load_without_404(server, browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    failed: list[str] = []
    assert sign_in(page, server["base"], "admin") == 200
    page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.goto(server["base"] + "/", wait_until="networkidle")
    ctx.close()
    assert not failed, failed


# ---------- шапка, треки, датчики ----------
def test_header_counters_follow_the_state(dash):
    dash.push(dash.state(tracks=[track(1, side="enemy"), track(2, side="enemy", coasting=True), track(3, side="friend")]))
    p = dash.page
    expect(p.locator("#s-tracks")).to_have_text("2")   # трек, що лише дотягується прогнозом (coasting), не рахуємо
    expect(p.locator("#s-enemy")).to_have_text("1")
    expect(p.locator("#s-tent")).to_have_text("1")
    expect(p.locator("#s-ev")).to_have_text("1234")
    expect(p.locator("#s-t")).to_have_text("12")
    n = len(dash.cfg["sensors"])
    expect(p.locator("#sens")).to_have_text(f"датчики: {n}/{n}")


def test_track_table_is_sorted_by_threat_and_friend_level_is_dimmed(dash):
    dash.push(dash.state(tracks=[track(5), track(7, level="CRITICAL"), track(6, level="WARNING", side="friend")]))
    rows = dash.page.locator("#tbody tr")
    expect(rows).to_have_count(3)
    assert [rows.nth(i).get_attribute("data-id") for i in range(3)] == ["7", "6", "5"]
    friend_level = rows.nth(1).locator("td").nth(2)
    expect(friend_level).to_have_text("увага")
    assert "opacity" in (friend_level.get_attribute("style") or "")


def test_unmeasured_altitude_is_shown_as_a_dash_not_as_assumed_metres(dash):
    dash.push(dash.state(tracks=[track(1, z=60.0, alt_known=False), track(2, z=83.4)]))
    unknown = dash.page.locator('#tbody tr[data-id="1"] td').nth(4)
    expect(unknown).to_have_text("—")
    assert "висоту не виміряно" in (unknown.get_attribute("title") or "")
    expect(dash.page.locator('#tbody tr[data-id="2"] td').nth(4)).to_have_text("83")


def test_clicking_a_track_row_selects_and_second_click_deselects(dash):
    dash.push(dash.state(tracks=[track(1), track(2)]))
    row = dash.page.locator('#tbody tr[data-id="2"]')
    row.click()
    expect(row).to_have_class("sel")
    row.click()
    expect(row).not_to_have_class("sel")


def test_lost_radar_shows_degraded_banner(dash):
    sensors = [{"id": s["id"], "label": s["kind"], "state": "LOST" if s["kind"] == "radar" else "OK", "age_s": 42.0}
               for s in dash.cfg["sensors"]]
    assert any(s["state"] == "LOST" for s in sensors), "у демо-конфігу має бути радар"
    dash.push(dash.state(sensors=sensors))
    banner = dash.page.locator("#degraded")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("без радара")
    expect(dash.page.locator("#sens")).to_have_class("chip off")
    expect(dash.page.locator("#sensors .sensor.LOST").first).to_contain_text("42 с")


def test_legend_toggles(dash):
    btn = dash.page.locator("#legend-toggle")
    expect(btn).to_have_attribute("aria-expanded", "false")
    btn.click()
    expect(btn).to_have_attribute("aria-expanded", "true")
    expect(dash.page.locator("#legend")).not_to_have_class("collapsed")


# ---------- тривоги ----------
def test_alerts_unreviewed_first_with_counter(dash):
    dash.push(dash.state(tracks=[track(1)], alerts=[
        alert(10, 1, status="confirmed", reviewed_by="oper", reviewed_at=1795352400.0),
        alert(11, 1, level="WARNING"),
        alert(12, 1, status="dismissed", reviewed_by="oper", reviewed_at=1795352400.0),
        alert(13, 1)]))
    cards = dash.page.locator("#alerts .alert")
    expect(cards).to_have_count(4)
    ids = [cards.nth(i).locator(".rv").get_attribute("data-id") for i in range(4)]
    assert ids == ["11", "13", "10", "12"]   # без рішення вгорі, далі в порядку сервера
    expect(cards.nth(2)).to_have_class("alert CRITICAL reviewed")
    expect(cards.nth(2).locator(".rv .st")).to_have_text("✓ підтверджено")
    expect(dash.page.locator("#alerts-new")).to_have_text("· без рішення: 2")


def test_no_alerts_shows_placeholder_and_hides_counter(dash):
    dash.push(dash.state(alerts=[alert(1, 1)]))
    expect(dash.page.locator("#alerts .alert")).to_have_count(1)
    dash.push(dash.state(alerts=[]))
    expect(dash.page.locator("#alerts .empty")).to_have_text("Тривог немає")
    expect(dash.page.locator("#alerts-new")).to_be_hidden()


def test_new_alert_raises_a_toast_but_the_first_frame_does_not(dash):
    dash.push(dash.state(alerts=[alert(1, 1)]))   # перший кадр: тривоги вже були до відкриття сторінки
    expect(dash.page.locator("#alerts .alert")).to_have_count(1)
    expect(dash.page.locator("#toast .toast")).to_have_count(0)
    dash.push(dash.state(alerts=[alert(2, 4, zone="Зона B"), alert(1, 1)]))
    toast = dash.page.locator("#toast .toast")
    expect(toast).to_have_count(1)
    expect(toast).to_have_class("toast CRITICAL")
    expect(toast).to_contain_text("#4")


def test_confirm_posts_the_decision_with_the_typed_note(dash):
    sent: list[dict[str, Any]] = []

    def review(route: Any) -> None:
        sent.append({"url": route.request.url, "body": json.loads(route.request.post_data or "{}")})
        route.fulfill(status=200, content_type="application/json", body="{}")
    dash.page.route("**/api/incidents/*/review", review)
    dash.push(dash.state(tracks=[track(1)], alerts=[alert(21, 1)]))
    card = dash.page.locator('.rv[data-id="21"]')
    card.locator("input[data-note]").fill("птах, не дрон")
    card.locator('button[data-review="dismissed"]').click()
    deadline = time.time() + 5
    while not sent and time.time() < deadline:
        dash.page.wait_for_timeout(50)
    assert sent and sent[0]["url"].endswith("/api/incidents/21/review")
    assert sent[0]["body"] == {"status": "dismissed", "note": "птах, не дрон"}


def test_failed_review_shows_a_warning_toast(dash):
    dash.page.route("**/api/incidents/*/review", lambda r: r.fulfill(status=500, body="boom"))
    dash.push(dash.state(alerts=[alert(30, 1)]))
    dash.page.locator('.rv[data-id="30"] button[data-review="confirmed"]').click()
    expect(dash.page.locator("#toast .toast.WARNING")).to_contain_text("Не вдалося зберегти рішення (HTTP 500)")


def test_comment_draft_and_focus_survive_a_new_alert(dash):
    dash.push(dash.state(alerts=[alert(40, 1)]))
    note = dash.page.locator('.rv[data-id="40"] input[data-note]')
    note.click()
    note.press_sequentially("пише оператор")
    dash.push(dash.state(alerts=[alert(41, 2), alert(40, 1)]))   # нова тривога перебудовує список
    expect(dash.page.locator("#alerts .alert")).to_have_count(2)
    note = dash.page.locator('.rv[data-id="40"] input[data-note]')
    expect(note).to_have_value("пише оператор")
    expect(note).to_be_focused()
    note.press_sequentially("!")
    expect(note).to_have_value("пише оператор!")


def test_untrusted_alert_text_is_escaped(dash):
    evil = '<img src=x onerror="window.__pwned=1">'
    dash.push(dash.state(alerts=[alert(50, 1, zone=evil, status="dismissed", reviewed_by=evil, reviewed_at=1795352400.0,
                                       review_note=evil)]))
    card = dash.page.locator("#alerts .alert")
    expect(card).to_contain_text(evil)
    assert dash.page.locator("#alerts img").count() == 0
    assert dash.page.evaluate("window.__pwned") is None


# ---------- AI-асистент ----------
def test_ai_answer_is_rendered_as_safe_markdown_with_tool_chips(dash):
    asked: list[Any] = []

    def ask(route: Any) -> None:
        asked.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"answer": "У зоні **2 цілі** <b>x</b>", "tool_calls": [{"name": "get_incident_stats"}]}))
    dash.page.route("**/api/ai/ask", ask)
    dash.page.locator("#ai-q-input").fill("що в зоні A?")
    dash.page.locator("#ai-form button").click()
    answer = dash.page.locator("#ai-log .ai-a")
    expect(answer.locator("strong")).to_have_text("2 цілі")
    expect(answer).to_contain_text("<b>x</b>")   # HTML від моделі лишається текстом
    expect(answer.locator(".tool-chip")).to_have_text("get_incident_stats")
    expect(dash.page.locator("#ai-log .ai-q")).to_have_text("що в зоні A?")
    assert asked == [{"question": "що в зоні A?"}]


def test_ai_unavailable_is_reported(dash):
    dash.page.route("**/api/ai/ask", lambda r: r.fulfill(status=503, body="{}"))
    dash.page.locator("#ai-q-input").fill("стан?")
    dash.page.locator("#ai-form button").click()
    expect(dash.page.locator("#ai-log .ai-a")).to_have_text("AI недоступний")


# ---------- ролі ----------
def test_viewer_sees_no_review_buttons_editor_or_ai(dash_as):
    d = dash_as("viewer")
    d.push(d.state(alerts=[alert(1, 1), alert(2, 1, status="confirmed", reviewed_by="oper", reviewed_at=1795352400.0)]))
    expect(d.page.locator("#alerts .alert")).to_have_count(2)
    expect(d.page.locator("button[data-review]")).to_have_count(0)
    expect(d.page.locator("input[data-note]")).to_have_count(0)
    expect(d.page.locator('.rv[data-id="2"] .st')).to_have_text("✓ підтверджено")   # підсумок рішення видно
    expect(d.page.locator("#edit-sensors")).to_be_hidden()
    expect(d.page.locator("#ai-box")).to_be_hidden()
    expect(d.page.locator("#audit-box")).to_be_hidden()
    expect(d.page.locator("#who-name")).to_have_text("viewer (перегляд)")
    assert not d.errors, d.errors


def test_operator_reviews_but_cannot_edit_layout(dash_as):
    d = dash_as("operator")
    d.push(d.state(alerts=[alert(1, 1)]))
    expect(d.page.locator("button[data-review]")).to_have_count(2)
    expect(d.page.locator("#ai-form")).to_be_visible()
    expect(d.page.locator("#edit-sensors")).to_be_hidden()
    expect(d.page.locator("#audit-box")).to_be_hidden()
    expect(d.page.locator("#catalog-body [draggable]")).to_have_count(0)
    assert not d.errors, d.errors


def test_forbidden_review_shows_insufficient_rights_toast(dash):
    dash.page.route("**/api/incidents/*/review", lambda r: r.fulfill(status=403, body="{}"))
    dash.push(dash.state(alerts=[alert(60, 1)]))
    dash.page.locator('.rv[data-id="60"] button[data-review="confirmed"]').click()
    expect(dash.page.locator("#toast .toast.WARNING")).to_contain_text("Недостатньо прав")


def test_admin_sees_audit_tab_with_integrity_ok(dash_as):
    d = dash_as("admin")
    expect(d.page.locator("#edit-sensors")).to_be_visible()
    expect(d.page.locator("#ai-form")).to_be_visible()
    expect(d.page.locator("#audit-box")).to_be_visible()
    d.page.locator("#audit-box summary").click()
    expect(d.page.locator("#audit-body tr", has_text="login").first).to_be_visible()
    expect(d.page.locator("#audit-integrity")).to_have_text("цілий на момент старту")
    assert not d.errors, d.errors


def test_audit_rows_are_rendered_as_text(dash_as, server):
    # Невідоме ім'я журнал не зберігає (там може бути пароль), а відомі імена конфіг обмежує безпечними символами. Тож
    # зловмисний рядок потрапляє в журнал через шлях відмовленого запиту (запис `denied`): клієнт керує ним повністю.
    evil = '<img src=x onerror=window.__pwned=1>'
    d0 = dash_as("viewer")
    status = d0.page.evaluate("""async (p) => (await fetch(p, {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"}, body: "{}"})).status""", f"/api/incidents/{evil}/review")
    assert status == 403
    d = dash_as("admin")
    d.page.locator("#audit-box summary").click()
    row = d.page.locator("#audit-body tr", has_text="denied").first
    expect(row).to_contain_text(evil)
    assert d.page.locator("#audit-body img").count() == 0
    assert d.page.evaluate("window.__pwned") is None
    assert not d.errors, d.errors


# ---------- відтворення інциденту ----------
def replay_payload(cfg: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    row = lambda x, lvl: [1, x, 20.0, 50.0, 1.0, 0.0, lvl, "e", 0, 1, 0]  # noqa: E731
    return {"incident_id": 7, "ts": 1795352316.0, "complete": complete, "v": 1, "step": 0.2,
            "target": {"track_id": 1, "pos": [10.0, 20.0, 50.0], "level": "WARNING", "zone": "A"},
            "setup": {"sensors": [{k: s[k] for k in ("id", "label", "pos", "max_range", "heading_deg", "fov_deg")} for s in cfg["sensors"]],
                      "zones": cfg["zones"], "origin": cfg["origin"]},
            "frames": [[-3.0, [row(8.0, 0)]], [-2.0, [row(9.0, 0)]], [0.0, [row(10.0, 1)]], [2.0, [row(11.0, 1)]]]}


def test_replay_plays_pauses_seeks_and_restores_live_view(dash):
    dash.page.route("**/api/incidents/7/replay", lambda r: r.fulfill(json=replay_payload(dash.cfg)))
    dash.push(dash.state(tracks=[track(1)], alerts=[alert(7, 1)]))
    dash.page.locator('button[data-replay="7"]').click()
    expect(dash.page.locator("#replay")).to_be_visible()
    expect(dash.page.locator("#rp-time")).to_contain_text("T")
    dash.page.locator("#rp-play").click()   # пауза
    expect(dash.page.locator("#rp-play")).to_have_text("▶")
    dash.page.locator("#rp-seek").fill("0")
    expect(dash.page.locator("#rp-time")).to_have_text("T−3 с")
    dash.page.locator("#rp-seek").fill("3")
    expect(dash.page.locator("#rp-time")).to_have_text("T+2 с")
    dash.page.locator("#rp-close").click()
    expect(dash.page.locator("#replay")).to_be_hidden()
    dash.push(dash.state(tracks=[track(2), track(3)], alerts=[alert(7, 1)]))
    expect(dash.page.locator("#tbody tr")).to_have_count(2)   # живий вигляд повернувся


def test_live_alerts_still_arrive_during_replay(dash):
    dash.page.route("**/api/incidents/7/replay", lambda r: r.fulfill(json=replay_payload(dash.cfg)))
    dash.push(dash.state(tracks=[track(1)], alerts=[alert(7, 1)]))
    dash.page.locator('button[data-replay="7"]').click()
    expect(dash.page.locator("#replay")).to_be_visible()
    dash.push(dash.state(tracks=[track(1)], alerts=[alert(8, 1, level="CRITICAL"), alert(7, 1)]))
    expect(dash.page.locator("#alerts .alert")).to_have_count(2)   # жива тривога не губиться під час відтворення
    dash.page.keyboard.press("Escape")
    expect(dash.page.locator("#replay")).to_be_hidden()


def test_missing_replay_gives_a_toast_not_a_player(dash):
    dash.page.route("**/api/incidents/7/replay", lambda r: r.fulfill(status=404, json={"detail": "немає"}))
    dash.push(dash.state(tracks=[track(1)], alerts=[alert(7, 1)]))
    dash.page.locator('button[data-replay="7"]').click()
    expect(dash.page.locator("#toast .toast.WARNING")).to_contain_text("Запису відтворення")
    expect(dash.page.locator("#replay")).to_be_hidden()


# ---------- розкладка: роздільник і відкріплені панелі ----------
def aside_width(page: Page) -> float:
    return float(page.evaluate("document.querySelector('aside').getBoundingClientRect().width"))


def test_splitter_resizes_the_side_panel_and_remembers_it(dash):
    page = dash.page
    assert abs(aside_width(page) - 348) < 2
    box = page.locator("#splitter").bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 3, y)
    page.mouse.down()
    page.mouse.move(box["x"] - 150, y, steps=5)
    page.mouse.up()
    assert 480 < aside_width(page) < 520
    page.reload()
    expect(page.locator("#conn")).to_have_text("на звʼязку", timeout=20_000)
    assert 480 < aside_width(page) < 520   # ширина пережила перезавантаження
    page.locator("#splitter").dblclick()
    assert abs(aside_width(page) - 348) < 2
    page.locator("#splitter").focus()
    page.keyboard.press("ArrowLeft")
    assert abs(aside_width(page) - 368) < 2   # з клавіатури: 20 px за натискання
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert abs(aside_width(page) - 328) < 2
    for _ in range(10):
        page.keyboard.press("Shift+ArrowRight")
    assert abs(aside_width(page) - 280) < 2   # менше мінімуму не стискається


def open_panel(dash: Dashboard, name: str) -> Dashboard:
    """Відкріплена панель у тому ж контексті (та сама сесія й BroadcastChannel), WebSocket підмінено так само."""
    page = dash.page.context.new_page()
    panel = Dashboard(page, dash.cfg)
    panel.errors = watch_errors(page)

    def on_ws(ws: WebSocketRoute) -> None:
        panel.ws = ws
    page.route_web_socket("**/ws", on_ws)
    page.goto(dash.page.url.split("/")[0] + "//" + dash.page.url.split("/")[2] + "/panel/" + name)
    expect(page.locator("#conn")).to_have_text("на звʼязку")
    return panel


def test_detach_button_opens_the_panel_in_its_own_window(dash):
    with dash.page.expect_popup() as popup:
        dash.page.locator('button[data-detach="alerts"]').click()
    assert popup.value.url.endswith("/panel/alerts")
    popup.value.close()


def test_detached_alerts_panel_shows_only_alerts_and_reviews_them(dash):
    panel = open_panel(dash, "alerts")
    errors = panel.errors
    panel.push(panel.state(tracks=[track(7, "CRITICAL", "enemy")], alerts=[alert(1, 7)]))
    expect(panel.page.locator("#alerts .alert")).to_have_count(1)
    expect(panel.page.locator("#mapwrap")).to_be_hidden()
    expect(panel.page.locator("#tracks")).to_be_hidden()
    expect(panel.page.locator("#sensors-box")).to_be_hidden()
    posted: list[Any] = []

    def review(route: Any) -> None:
        posted.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")
    panel.page.route("**/api/incidents/1/review", review)
    panel.page.locator('#alerts button[data-review="confirmed"]').click()
    deadline = time.time() + 5
    while not posted and time.time() < deadline:
        panel.page.wait_for_timeout(50)
    assert posted == [{"status": "confirmed", "note": None}]
    assert not errors, "\n".join(errors)


def test_clicks_in_detached_panels_drive_the_main_window(dash):
    tracks = [track(1), track(2, "CRITICAL", "enemy")]
    dash.push(dash.state(tracks=tracks, alerts=[alert(1, 2)]))
    tracks_panel = open_panel(dash, "tracks")
    alerts_panel = open_panel(dash, "alerts")
    for p in (tracks_panel, alerts_panel):
        p.push(p.state(tracks=tracks, alerts=[alert(1, 2)]))
    expect(alerts_panel.page.locator("#alerts .alert")).to_have_count(1)
    expect(tracks_panel.page.locator('#tbody tr[data-id="1"]')).to_be_visible()
    expect(tracks_panel.page.locator("#alerts")).to_be_hidden()
    tracks_panel.page.locator('#tbody tr[data-id="1"]').click()
    expect(dash.page.locator('#tbody tr[data-id="1"]')).to_have_class("sel")   # головне вікно виділило ціль
    alerts_panel.page.locator("#alerts .alert .id").click()
    dash.push(dash.state(tracks=tracks, alerts=[alert(1, 2)]))
    expect(dash.page.locator('#tbody tr[data-id="2"]')).to_have_class("sel")
    for p in (tracks_panel, alerts_panel):
        assert not p.errors, "\n".join(p.errors)
