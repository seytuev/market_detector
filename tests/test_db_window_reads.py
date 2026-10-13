"""Этап 7 (спека §12): tail-чтения канала свечей ограничены окном в SQL
(DESC LIMIT по PK (instrument_id, timeframe, open_time)), а не срезом
полной истории в Python. Проверки:

  a) get_candles(limit=N) возвращает ПОСЛЕДНИЕ N свечей в восходящем
     порядке — семантика «rows[-N:]» без полного чтения;
  b) EXPLAIN QUERY PLAN ограниченного запроса идёт по индексу PK
     (SEARCH ... USING INDEX), полного SCAN таблицы candle нет;
  c) горячие вызыватели (channel_status, _continuity_channel) просят у БД
     ограниченное окно — проверяется шпионом на get_candles;
  d) граница окна инкрементальной обработки (_process_synced_channel,
     start_ms = cursor - step + 2) не теряет ни одной свечи с
     close_time > cursor — эквивалентность полному скану с фильтром.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import DetectorConfig, Settings
from app.db import Database
from app.models import TIMEFRAME_MINUTES
from app.services.data_sync import DataSync
from app.services.quality import _CONTINUITY_SCAN, _continuity_channel

from .conftest import make_candle

H1_MS = TIMEFRAME_MINUTES["H1"] * 60_000
BASE = 1_790_000_000_000  # произвольная точка на сетке H1 (кратна H1_MS)


def _fill(db: Database, iid: int, count: int, tf: str = "H1") -> list:
    step = TIMEFRAME_MINUTES[tf] * 60_000
    candles = [
        make_candle(BASE + i * step, 100 + i, 101 + i, 99 + i, 100.5 + i,
                    timeframe=tf, instrument_id=iid)
        for i in range(count)
    ]
    db.insert_candles(candles)
    return candles


def test_limit_returns_last_n_ascending(db: Database, instrument_id: int):
    candles = _fill(db, instrument_id, 10)
    full = db.get_candles(instrument_id, "H1")
    limited = db.get_candles(instrument_id, "H1", limit=3)
    assert [c.open_time for c in limited] == [c.open_time for c in candles[-3:]]
    assert [c.open_time for c in limited] == [c.open_time for c in full[-3:]]
    # порядок восходящий, поля совпадают с полным чтением
    assert limited == full[-3:]
    # limit больше истории — вся история; limit=1 — последняя закрытая
    assert db.get_candles(instrument_id, "H1", limit=50) == full
    assert db.get_candles(instrument_id, "H1", limit=1) == full[-1:]


def test_limit_honors_closed_only_and_window(db: Database, instrument_id: int):
    candles = _fill(db, instrument_id, 5)
    forming = make_candle(BASE + 5 * H1_MS, 105, 106, 104, 105.5,
                          timeframe="H1", instrument_id=instrument_id,
                          closed=False)
    db.insert_candles([forming])
    # closed_only по умолчанию: формирующаяся не вытесняет последнюю закрытую
    tail = db.get_candles(instrument_id, "H1", limit=2)
    assert [c.open_time for c in tail] == [c.open_time for c in candles[-2:]]
    # closed_only=False: формирующаяся — последняя
    tail_open = db.get_candles(instrument_id, "H1", limit=2, closed_only=False)
    assert [c.open_time for c in tail_open] == [
        candles[-1].open_time, forming.open_time]
    # limit сочетается с окном start_ms/end_ms
    windowed = db.get_candles(instrument_id, "H1",
                              start_ms=candles[1].open_time,
                              end_ms=candles[4].open_time, limit=2)
    assert [c.open_time for c in windowed] == [
        candles[3].open_time, candles[4].open_time]


def test_limited_query_uses_pk_index_no_scan(db: Database, instrument_id: int):
    _fill(db, instrument_id, 300)
    # точная форма запроса из Database.get_candles(limit=...)
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM candle "
        "WHERE instrument_id=? AND timeframe=? AND closed=1 "
        "ORDER BY open_time DESC LIMIT ?",
        (instrument_id, "H1", 200),
    ).fetchall()
    details = [" ".join(str(part) for part in tuple(r)) for r in plan]
    search = [d for d in details if "SEARCH" in d and "candle" in d.lower()]
    assert search, f"ожидали SEARCH по индексу candle, план: {details}"
    assert any("USING INDEX" in d or "PRIMARY KEY" in d for d in search), details
    bare_scan = [d for d in details
                 if "SCAN" in d and "candle" in d.lower() and "USING" not in d]
    assert not bare_scan, f"полный SCAN таблицы candle в ограниченном чтении: {details}"


def test_channel_status_reads_bounded_tail(db: Database, instrument_id: int, monkeypatch):
    _fill(db, instrument_id, 5)
    # DataSync с минимальным worker-контекстом: channel_status читает только
    # БД и настройки (cfg.ltf_poll_seconds / settings.poll_seconds)
    stub = SimpleNamespace(db=db, settings=Settings(), cfg=DetectorConfig())
    sync = DataSync(stub)
    calls = []
    original = db.get_candles

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "get_candles", spy)
    sync.channel_status(db.get_instrument(instrument_id), "H1")
    tail_calls = [c for c in calls if c[1].get("limit") is not None]
    assert tail_calls, f"channel_status обязан читать ограниченное окно: {calls}"
    assert all(c[1]["limit"] == 200 for c in tail_calls)


def test_continuity_channel_reads_bounded_tail(db: Database, instrument_id: int, monkeypatch):
    _fill(db, instrument_id, 5)
    calls = []
    original = db.get_candles

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "get_candles", spy)
    channel = _continuity_channel(db, instrument_id)
    assert channel["status"] == "ok"
    assert [c[1].get("limit") for c in calls] == [_CONTINUITY_SCAN]


def test_cursor_window_bound_matches_full_scan(db: Database, instrument_id: int):
    """Математика границы worker._process_synced_channel: close_time > cursor
    ⟺ open_time >= cursor - step + 2 (close_time = open_time + step - 1);
    окно в SQL + фильтр-страж эквивалентны полному скану с фильтром."""
    candles = _fill(db, instrument_id, 100)
    full = db.get_candles(instrument_id, "H1")
    # курсор — close_time реальной свечи (как в meta data_sync:processed)
    for cursor in (0, candles[0].close_time, candles[-10].close_time,
                   candles[-1].close_time):
        start_ms = max(0, cursor - H1_MS + 2)
        expected = [c.open_time for c in full if c.close_time > cursor]
        got = [c.open_time
               for c in db.get_candles(instrument_id, "H1", start_ms=start_ms)
               if c.close_time > cursor]
        assert got == expected, f"cursor={cursor}"
