"""Спільні фікстури тестів сервісу."""
import contextlib

import pytest

# Робочий стан сервісу лежить в app.state (runtime, access), а FastAPI-об'єкт app.app один на процес. Окремий процес на файл
# це приховував; в одному процесі стан одного тесту (ввімкнений вхід, підставлений рушій) витікав би в наступні.
_STATE_ATTRIBUTES = ("runtime", "access")


@pytest.fixture(autouse=True)
def isolated_app_state():
    """Прибирає стан із app.state після кожного тесту. Без залежностей сервісу нічого не робить."""
    try:
        from dronewatch.service import app
    except ImportError:
        yield
        return
    yield
    for name in _STATE_ATTRIBUTES:
        with contextlib.suppress(AttributeError, KeyError):   # Starlette State кидає KeyError для відсутнього атрибута
            delattr(app.app.state, name)
