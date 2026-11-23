"""Запис для відтворення інцидентів: що робили всі цілі за хвилину до тривоги й хвилину після неї.

Рушій кладе в кільцевий буфер кадр раз на `step_s` (усі підтверджені цілі: положення, швидкість, рівень, приналежність).
Коли спрацьовує тривога, знімок буфера за останні `before_s` секунд одразу пишеться в `incident_replays` (щоб збій сервісу
не з'їв запис), а `after_s` секунд потому доповнюється й дописується з позначкою `complete`. У спокійний час на диск
нічого не потрапляє. Блок кожного інциденту обмежений (кадри, цілі в кадрі), записи старші за `retention_days` прибираються.

Запис ніколи не ламає трекінг: будь-який збій тут лише логується (див. `Engine.tick`). Розстановка датчиків і зони на момент
інциденту зберігаються разом із кадрами, бо потім розстановку можуть змінити."""
import json
import logging
import sqlite3
import zlib
from collections import deque
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("dronewatch.replay")
FORMAT = 1
MAX_BLOB_BYTES = 32 * 1024 * 1024   # стеля розпакованого блока при читанні: зіпсований запис не роздуває памʼять
LEVEL_CODE = {"NONE": 0, "WARNING": 1, "CRITICAL": 2}
SIDE_CODE = {"enemy": "e", "friend": "f"}   # решта: "u"


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    before_s: float = 60.0
    after_s: float = 60.0
    step_s: float = 1.0
    retention_days: float = 30.0
    max_tracks_per_frame: int = 100   # понад це лишаються найнебезпечніші (рівень, далі номер)


@dataclass(slots=True)
class _Capture:
    incident_id: int
    ts: float
    setup: dict[str, Any]
    frames: list[tuple[float, list[list[Any]]]]
    until: float
    target: dict[str, Any]


class ReplayRecorder:
    def __init__(self, db: sqlite3.Connection, opts: ReplayOptions) -> None:
        self.db, self.opts = db, opts
        self._ring: deque[tuple[float, list[list[Any]]]] = deque()
        self._pending: dict[int, _Capture] = {}
        self._last = float("-inf")
        self._now = float("-inf")   # час останнього скану: за ним рахується термін зберігання
        self._max_frames = int((opts.before_s + opts.after_s) / opts.step_s) + 10
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db.execute("""CREATE TABLE IF NOT EXISTS incident_replays (
            incident_id INTEGER PRIMARY KEY, ts REAL, complete INTEGER, data BLOB)""")
        self.db.commit()

    def rebind(self, db: sqlite3.Connection) -> None:
        """Той самий кільцевий буфер, інша БД (HA: резерв пише в памʼять, лідер у файл). Буфер кадрів не губиться."""
        self.db = db
        self._ensure_table()

    def on_scan(self, ts: float, frame: Callable[[], list[list[Any]]], new_alerts: list[Any],
                setup: Callable[[], dict[str, Any]]) -> None:
        """Після кожного скану (під замком рушія). Кадр береться раз на step_s або одразу, коли є нова тривога."""
        self._now = ts
        due = ts - self._last >= self.opts.step_s
        if not (due or new_alerts or self._pending):
            return
        snap: list[list[Any]] | None = frame() if (due or new_alerts) else None
        if snap is not None and due:
            self._last = ts
            self._ring.append((ts, snap))
        while self._ring and ts - self._ring[0][0] > self.opts.before_s:
            self._ring.popleft()
        for a in new_alerts:
            if not a["id"]:
                continue
            frames = list(self._ring)
            if snap is not None and (not frames or frames[-1][0] != ts):
                frames.append((ts, snap))   # кадр самої тривоги, навіть якщо крок ще не минув
            cap = _Capture(a["id"], a["ts"], setup(), frames, a["ts"] + self.opts.after_s,
                           {"track_id": a.get("track_id"), "pos": a.get("pos"), "level": a.get("level"), "zone": a.get("zone")})
            self._pending[a["id"]] = cap
            self._write(cap, complete=False)
        if due and snap is not None:
            for cap in list(self._pending.values()):
                if (not cap.frames or cap.frames[-1][0] != ts) and len(cap.frames) < self._max_frames:
                    cap.frames.append((ts, snap))
        for cap in list(self._pending.values()):
            if ts >= cap.until or len(cap.frames) >= self._max_frames:
                self._finish(cap)

    def flush(self) -> None:
        """Новий сеанс трекінгу: номери цілей після скидання починаються заново, тож незавершені записи дописуємо як є."""
        for cap in list(self._pending.values()):
            self._finish(cap)
        self._ring.clear()
        self._last = float("-inf")

    def _finish(self, cap: _Capture) -> None:
        self._pending.pop(cap.incident_id, None)
        self._write(cap, complete=True)
        self._purge(self._now)

    def _write(self, cap: _Capture, *, complete: bool) -> None:
        payload = {"v": FORMAT, "step": self.opts.step_s, "target": cap.target, "setup": cap.setup,
                   "frames": [[round(t - cap.ts, 2), tr] for t, tr in cap.frames]}
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
        self.db.execute("INSERT OR REPLACE INTO incident_replays (incident_id, ts, complete, data) VALUES (?,?,?,?)",
                        (cap.incident_id, cap.ts, int(complete), blob))
        self.db.commit()

    def _purge(self, now: float) -> None:
        self.db.execute("DELETE FROM incident_replays WHERE ts < ?", (now - self.opts.retention_days * 86400,))
        self.db.commit()


def read_replay(db_path: str, incident_id: int) -> dict[str, Any] | None:
    """Запис відтворення інциденту або None (запису немає). `complete` false: після тривоги ще дописується."""
    with closing(sqlite3.connect(db_path)) as db:
        row = db.execute("SELECT ts, complete, data FROM incident_replays WHERE incident_id=?", (incident_id,)).fetchone()
    if row is None:
        return None
    ts, complete, blob = row
    unpacker = zlib.decompressobj()
    raw = unpacker.decompress(blob, MAX_BLOB_BYTES)
    if unpacker.unconsumed_tail:
        raise ValueError("запис відтворення завеликий")
    data = json.loads(raw)
    return {"incident_id": incident_id, "ts": ts, "complete": bool(complete), **data}
