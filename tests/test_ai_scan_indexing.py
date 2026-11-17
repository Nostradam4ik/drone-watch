"""scan_step індексує нові тривоги в Qdrant по факту появи, незалежно від наративу (якого ще немає — Фаза 2).
Рушій тут підмінений на canned tick(), що повертає заздалегідь задану тривогу: natural-альтернатива (розігнати
симуляцію до підтвердженого треку за кілька сканів) тестувала б трекер, а не гачок індексації, який є предметом
цього завдання — трекер і так покритий tests/test_health.py."""
import asyncio

from dronewatch.fusion.sim import default_sensors
from dronewatch.service import app
from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.dashboard_auth import LoginLimiter
from dronewatch.service.engine import Engine, Zone

ALERT = {"ts": 1.0, "track_id": 7, "level": "WARNING", "zone": "z", "pos": [1.0, 2.0, 0.0], "speed": 5.0, "sensors": ["radar1"]}


def _ai_runtime(index):
    return app.AiRuntime(llm=object(), index=index, max_iterations=4, limiter=LoginLimiter())


def test_scan_step_indexes_new_alerts_when_ai_enabled():
    indexed = []

    class _RecordingIndex:
        def index(self, point_id, row):
            indexed.append((point_id, row))

    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 50, "WARNING")]), ai=_ai_runtime(_RecordingIndex()))
    rt.engine.tick = lambda: [ALERT]
    asyncio.run(app.scan_step(app.app, rt))
    assert indexed and indexed[0][1]["zone"] == "z" and indexed[0][1]["sensors"] == "radar1"


def test_scan_step_skips_indexing_when_ai_disabled():
    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 50, "WARNING")]))  # ai=None за замовчуванням
    rt.engine.tick = lambda: [ALERT]
    asyncio.run(app.scan_step(app.app, rt))  # не повинно впасти лише через те, що rt.ai немає


def test_scan_step_survives_qdrant_indexing_failure():
    class _BrokenIndex:
        def index(self, point_id, row):
            raise AiUnavailable("down")

    rt = app.Runtime(engine=Engine(default_sensors(), [Zone("z", 50, "WARNING")]), ai=_ai_runtime(_BrokenIndex()))
    rt.engine.tick = lambda: [ALERT]
    asyncio.run(app.scan_step(app.app, rt))  # не повинно впасти й не повинно зупинити цикл сканів
    assert rt.scan.errors == 0, "збій AI-індексації не повинен рахуватись як помилка скану"
