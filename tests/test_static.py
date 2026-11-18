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
