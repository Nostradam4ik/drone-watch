"""Бібліотеки дашборда (MapLibre, Three.js) віддаються з сервісу, а не з CDN: лише відомі файли, жодних шляхів із клієнта."""
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dronewatch.service.app import VENDOR_FILES, app

STATIC = Path(__file__).resolve().parent.parent / "src" / "dronewatch" / "service" / "static"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("name", sorted(VENDOR_FILES))
def test_known_vendor_files_are_served_with_the_right_type(client, name):
    r = client.get(f"/static/vendor/{name}")
    assert r.status_code == 200 and len(r.content) > 10_000
    assert r.headers["content-type"].split(";")[0] == VENDOR_FILES[name]


@pytest.mark.parametrize("name", ["app.py", "../app.py", "..%2Fapp.py", "%2e%2e/app.py", "config.yaml", "nope.js", "vendor", ""])
def test_anything_else_is_not_found(client, name):
    assert client.get(f"/static/vendor/{name}").status_code in (404, 405)


def test_every_whitelisted_file_exists_on_disk():
    for name in VENDOR_FILES:
        assert (STATIC / "vendor" / name).is_file(), name


def test_dashboard_page_has_no_third_party_scripts():
    """Код сторінки — лише з нашого сервера. Шрифти (CSS) і дані тайлів карти йдуть ззовні, але це не виконуваний код."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" not in html and "unpkg.com" not in html
    assert '<script src="/static/vendor/maplibre-gl.js">' in html


def test_vendor_files_are_packaged_into_the_wheel(tmp_path):
    """Регресія: package-data `static/*` не захоплює підтеку vendor/, і образ Docker (pip install .) лишався без карти."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(tmp_path), str(root)], check=True,
                   capture_output=True)
    names = zipfile.ZipFile(next(tmp_path.glob("dronewatch*.whl"))).namelist()
    for name in VENDOR_FILES:
        assert f"dronewatch/service/static/vendor/{name}" in names, name


# ---------- шрифти й карта: нічого зовнішнього ----------
def test_pages_make_no_requests_to_google_fonts():
    for name in ("index.html", "login.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert "googleapis" not in html and "gstatic" not in html, name
        assert "/static/webfonts/fonts.css" in html, name


def test_webfonts_are_served_and_only_by_plain_name(client):
    css = client.get("/static/webfonts/fonts.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "googleapis" not in css.text and "gstatic" not in css.text
    names = [line.split("url(")[1].split(")")[0] for line in css.text.splitlines() if "src: url(" in line]
    assert names, "у fonts.css немає жодного @font-face"
    for n in names:
        r = client.get(f"/static/webfonts/{n}")
        assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and len(r.content) > 1000, n
    for bad in ("../app.py", "..%2Fapp.py", "x.txt", "nope.woff2", "google.css"):
        assert client.get(f"/static/webfonts/{bad}").status_code == 404, bad


def test_map_glyphs_only_for_the_bundled_font_and_numeric_ranges(client):
    ok = client.get("/static/fonts/Noto Sans Regular/0-255.pbf")
    assert ok.status_code == 200 and ok.headers["content-type"] == "application/x-protobuf" and len(ok.content) > 1000
    assert client.get("/static/fonts/Other Font/0-255.pbf").status_code == 404
    # діапазон, якого нема серед збережених: порожній валідний файл, а не 404 (404 ламає шар підписів на карті)
    missing = client.get("/static/fonts/Noto Sans Regular/11008-11263.pbf")
    assert missing.status_code == 200 and missing.content == b"" and missing.headers["content-type"] == "application/x-protobuf"
    for bad in ("abc-def", "..%2F0-255", "0-255x", "999999-1"):
        assert client.get(f"/static/fonts/Noto Sans Regular/{bad}.pbf").status_code == 404, bad


def make_runtime(tmp_path, monkeypatch, **map_cfg):
    from dronewatch.service import config
    from dronewatch.service.app import build_runtime
    from dronewatch.service.dashboard_web import DashboardAccess
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo", map=config.MapCfg(**map_cfg)))
    app.state.access = DashboardAccess()
    return app.state.runtime


def test_map_mode_is_offline_only_when_the_file_exists_and_online_only_by_consent(client, tmp_path, monkeypatch):
    tiles = tmp_path / "paris.pmtiles"
    make_runtime(tmp_path, monkeypatch, pmtiles=str(tiles))
    assert client.get("/api/config").json()["map"]["mode"] == "none"            # файлу немає, онлайн не дозволено: нічого зовнішнього
    assert client.get("/map/paris.pmtiles").status_code == 404
    make_runtime(tmp_path, monkeypatch, pmtiles=str(tiles), online_tiles=True)
    assert client.get("/api/config").json()["map"]["mode"] == "online"          # лише з явного дозволу
    tiles.write_bytes(bytes(range(256)) * 8)
    make_runtime(tmp_path, monkeypatch, pmtiles=str(tiles), online_tiles=True)
    assert client.get("/api/config").json()["map"]["mode"] == "offline"         # власний файл переважає онлайн


def test_pmtiles_file_supports_range_requests(client, tmp_path, monkeypatch):
    tiles = tmp_path / "paris.pmtiles"
    data = bytes(range(256)) * 8
    tiles.write_bytes(data)
    make_runtime(tmp_path, monkeypatch, pmtiles=str(tiles))
    r = client.get("/map/paris.pmtiles", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == data[10:20]
    assert client.get("/map/paris.pmtiles").content == data
