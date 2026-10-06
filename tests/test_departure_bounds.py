"""T02/T17 (ТЗ 06.10.2026 §3.1, §3.6): выход из базы не раньше её завершения.

Воспроизводит дефект зон №550/549/290/22/553/23: при replay свечи до
формирования базы попадали в фазовую машину кандидата и писали departed_at
раньше formed_at. Нижняя граница — закрытие последней свечи базы.
"""
from app.engine.scanner import Scanner
from app.models import (
    Direction,
    EventKind,
    TIMEFRAME_MINUTES,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
)
from tests.conftest import make_candle

W1_MS = TIMEFRAME_MINUTES["W1"] * 60_000
T0 = 1_700_000_000_000


def _w1(i: int, o: float, h: float, l: float, c: float):
    return make_candle(T0 + i * W1_MS, o, h, l, c, timeframe="W1")


def _ob_candidate(zid=None, confirmed_at=None, evidence=None):
    return Zone(
        id=zid, instrument_id=1, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="W1", lower=100.0, upper=110.0,
        formed_at=T0 + 10 * W1_MS, confirmed_at=confirmed_at,
        status=ZoneStatus.CANDIDATE, source="auto",
        source_candles=[T0 + 10 * W1_MS, T0 + 11 * W1_MS],
        evidence=evidence or {}, created_at=T0,
    )


def test_departure_not_before_base_end(db, cfg, instrument_id):
    """Свечи до базы целиком выше U не становятся «выходом» кандидата."""
    zid = db.insert_zone(_ob_candidate())
    candles = [
        # недели 0-9: цена далеко выше будущей базы — раньше это писало
        # departed_at в прошлое
        *[_w1(i, 200, 210, 195, 205) for i in range(10)],
        # база (недели 10-11)
        _w1(10, 105, 109, 101, 103),
        _w1(11, 103, 110, 100, 108),
        # неделя 12: реальный уход вверх после завершения базы
        _w1(12, 150, 160, 145, 155),
    ]
    db.insert_candles(candles)
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={"W1"})

    zone = db.get_zone(zid)
    base_end = close_boundary_ms(T0 + 11 * W1_MS, "W1")
    departed_at = zone.evidence.get("departed_at")
    assert zone.evidence.get("phase") == "departed"
    assert departed_at is not None and departed_at > base_end
    # ранние свечи не записаны как тесты/визиты до конца базы
    for v in db.get_visits(zid):
        assert v.entered_at > base_end


def test_no_departure_recorded_from_pre_base_history(db, cfg, instrument_id):
    """Если после базы выхода не было, departed_at остаётся пустым — ранняя
    история его не синтезирует."""
    zid = db.insert_zone(_ob_candidate())
    db.insert_candles([_w1(i, 200, 210, 195, 205) for i in range(10)])
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={"W1"})

    zone = db.get_zone(zid)
    assert zone.evidence.get("phase") != "departed"
    assert zone.evidence.get("departed_at") is None


def test_corrupted_departed_at_yields_no_signals(db, cfg, instrument_id):
    """Испорченное ранее состояние (departed_at не позднее конца базы) не
    порождает событий и помечается inconsistent/evidence_incomplete (T02)."""
    base_end = close_boundary_ms(T0 + 11 * W1_MS, "W1")
    confirmed_at = base_end + W1_MS
    zid = db.insert_zone(_ob_candidate(
        confirmed_at=confirmed_at,
        evidence={"phase": "departed", "departed_at": T0 + 5 * W1_MS},
    ))
    candles = [
        _w1(10, 105, 109, 101, 103),
        _w1(11, 103, 110, 100, 108),
        # после подтверждения цена входит в зону — обычно это TOUCH
        _w1(13, 115, 116, 104, 106),
    ]
    db.insert_candles(candles)
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={"W1"})

    zone = db.get_zone(zid)
    assert zone.evidence.get("integrity") == "inconsistent"
    assert zone.evidence.get("departure_evidence_incomplete") is True
    assert db.get_events(zid) == []


def test_close_boundary_semantics():
    """T17: boundary = close_time + 1 мс (exclusive close boundary)."""
    c = _w1(0, 1, 2, 0.5, 1.5)
    assert close_boundary_ms(c.open_time, "W1") == c.close_time + 1
