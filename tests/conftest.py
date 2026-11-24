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


# --- сховище: тести, що мають пройти на обох бекендах (pg пропускається без DRONEWATCH_TEST_PG_DSN)
import os


def pg_dsn() -> str | None:
    return os.environ.get("DRONEWATCH_TEST_PG_DSN")


@pytest.fixture(params=["sqlite", "pg"])
def db_backend(request, tmp_path):
    """Store із чистою схемою: sqlite у тимчасовому файлі, pg у БД з DRONEWATCH_TEST_PG_DSN (схема public скидається)."""
    from dronewatch.service.store import PostgresStore, SqliteStore
    if request.param == "sqlite":
        return SqliteStore(str(tmp_path / "t.db"))
    dsn = pg_dsn()
    if not dsn:
        pytest.skip("DRONEWATCH_TEST_PG_DSN не задано")
    pytest.importorskip("psycopg")
    store = PostgresStore(dsn, None, 2, 3)
    db = store.connect()
    db.execute("DROP SCHEMA public CASCADE")
    db.execute("CREATE SCHEMA public")
    db.commit()
    db.close()
    return store


@pytest.fixture
def pg_store():
    """Те саме, що db_backend[pg], але лише PostgreSQL (для тестів, що перевіряють саме його: права, тригери, блокування)."""
    dsn = pg_dsn()
    if not dsn:
        pytest.skip("DRONEWATCH_TEST_PG_DSN не задано")
    pytest.importorskip("psycopg")
    from dronewatch.service.store import PostgresStore
    store = PostgresStore(dsn, None, 2, 3)
    db = store.connect()
    db.execute("DROP SCHEMA public CASCADE")
    db.execute("CREATE SCHEMA public")
    db.commit()
    db.close()
    return store
