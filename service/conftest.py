"""Спільні фікстури тестів сервісу."""
import copy

import pytest

# Модуль app тримає робочий стан у глобальних змінних, а FastAPI-об'єкт app.app один на процес. Окремий процес на файл
# це приховував; в одному процесі стан одного тесту (ввімкнений вхід, підставлений рушій, база) витікав у наступні.
_APP_GLOBALS = ("engine", "demo_src", "mqtt_ingest", "clients", "scan_task", "scan_health", "DEMO", "DB_PATH")


@pytest.fixture(autouse=True)
def isolated_app_state():
    """Повертає стан модуля app у вихідний після кожного тесту. Без залежностей сервісу нічого не робить."""
    try:
        import app
    except ImportError:
        yield
        return
    saved = {name: copy.copy(getattr(app, name)) for name in _APP_GLOBALS}
    saved_access = getattr(app.app.state, "access", None)
    yield
    for name, value in saved.items():
        setattr(app, name, value)
    app.app.state.access = saved_access
