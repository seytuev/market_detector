"""§18: контекстный допуск FVG вне Premium — «шорт от LTF FVG вне Premium
после снятия SSL + теста 50% 1D FVG».

Синтетическая серия: восходящая структура с low-pivot 9.4 (idx3, SSL) и
первым high-pivot 12.5 (idx4), HH 17 (idx10) → медвежий BOS (idx15) с
тремя FVG движения выше будущего диапазона → снятие SSL 9.4 (idx16:
Low < 9.4, Close > 9.4) → экстремум 7.5 (idx18) достигает 50% бычьей
D1 FVG [7.0;8.5] → диапазон v1 [7.6;9.6] (idx25): все три FVG движения
вне Premium (выше R_high) и допускаются по контексту → касание
FVG [10.0;10.5] (idx26) с теми же аннотациями.
"""
from __future__ import annotations

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.notify.ltf_queue import LtfDispatcher
from app.notify.ltf_templates import LtfContext, render_ltf_messages
from tests.conftest import H1_MS
from tests.test_ltf_breaks import _series

T0 = 1_780_000_000_000

SERIES_CTX_HL = [
    (11.5, 10.5), (11.0, 10.0), (10.6, 9.6), (11.0, 9.4), (12.5, 10.0),
    (11.5, 10.2), (11.8, 10.4), (12.0, 10.6), (13.0, 10.8), (15.0, 12.0),
    (17.0, 12.5), (16.0, 13.5), (14.5, 12.0), (13.0, 10.5), (11.0, 9.6),
    (10.0, 8.6), (9.8, 8.4), (9.2, 8.0), (8.6, 7.5), (9.0, 7.8),
    (9.6, 8.2), (9.4, 8.0), (9.0, 7.2), (8.4, 7.8), (8.6, 8.0),
    (8.8, 8.2), (10.2, 9.7),
]
SERIES_CTX_CLOSES = {15: 8.9, 16: 9.5, 26: 9.5}

# D1 FVG бычья ниже: 50% = 7.25 достигнут экстремумом 7.2 (idx22)
D1_FVG = (6.5, 8.0)


def _setup(db: Database, instrument_id: int) -> int:
    """Родительский bear OB D1 и бычья D1 FVG-зона (цель теста 50%)."""
    db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=16.0, upper=18.0,
        formed_at=T0 - 10_000_000, confirmed_at=T0 - 9_000_000,
        status=ZoneStatus.ACTIVE,
    ))
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1",
        lower=D1_FVG[0], upper=D1_FVG[1],
        formed_at=T0 - 8_000_000, confirmed_at=T0 - 7_000_000,
        status=ZoneStatus.ACTIVE,
    ))


def _feed(db: Database, engine: LtfEngine, instrument_id: int, candles, upto: int):
    for c in candles[: upto + 1]:
        db.insert_candles([c])
        engine.process_h1_close(instrument_id, now_ms=c.close_time)


def _events(db: Database, obs_id: int):
    return sorted(db.list_ltf_events(observation_id=obs_id, limit=1000),
                  key=lambda e: (e.occurred_at, e.id))


def _ctx(db: Database, obs, sc) -> LtfContext:
    obs = db.get_ltf_observation(obs.id)
    return LtfContext(
        instrument=db.get_instrument(obs.instrument_id),
        zone=db.get_zone(obs.zone_id),
        observation=obs,
        scenario=db.get_ltf_scenario(sc.id) if sc else None,
    )


def test_short_fvg_outside_premium_ssl_fvg50_context(
    db: Database, cfg, instrument_id: int
):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_CTX_HL, SERIES_CTX_CLOSES, instrument_id)
    d1fvg_id = _setup(db, instrument_id)
    parent = db.get_zones(instrument_id=instrument_id,
                          types=[ZoneType.OB])[0]
    obs = engine.on_htf_zone_touched(instrument_id, parent, occurred_at=T0)

    # idx15: первичный медвежий BOS (закрытие 8.9 < HL 9.4), диапазона ещё нет
    _feed(db, engine, instrument_id, candles, 15)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.trigger == "BOS" and sc.direction == Direction.BEAR
    assert [e.kind for e in _events(db, obs.id)] == ["bos"]

    # idx16: снятие SSL 9.4 (Low 8.4 < 9.4, Close 9.5 > 9.4) — первый факт
    _feed(db, engine, instrument_id, candles, 16)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == ["bos", "context_update"]
    sweep = evs[-1]
    assert sweep.payload["fact"] == "counter_sweep"
    assert sweep.payload["level"] == 9.4
    assert sweep.payload["level_type"] == "SSL"
    assert sweep.occurred_at == candles[16].close_time

    # idx22: экстремум 7.2 достиг 50% бычьей D1 FVG — второй факт
    _feed(db, engine, instrument_id, candles, 22)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == ["bos", "context_update", "context_update"]
    fvg50 = evs[-1]
    assert fvg50.payload["fact"] == "htf_fvg50"
    assert fvg50.payload["zone_id"] == d1fvg_id
    assert fvg50.payload["tf"] == "D1"

    # idx25: диапазон v1 [7.2;9.6]; FVG движения выше R_high (outside_pd)
    # допущены по контексту — в entries_ready с outside_premium и half
    _feed(db, engine, instrument_id, candles, 25)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == [
        "bos", "context_update", "context_update", "entries_ready",
    ]
    ready = evs[-1]
    assert (ready.payload["range"]["lower"],
            ready.payload["range"]["upper"]) == (7.2, 9.6)
    by_lower = {e["lower"]: e for e in ready.payload["entries"]}
    assert set(by_lower) == {9.6, 10.0, 11.0, 13.0}
    # якорь пары BSL 9.6 — обычный допуск (в Premium)
    assert by_lower[9.6]["type"] == "BSL"
    assert "outside_premium" not in by_lower[9.6]
    assert by_lower[9.6]["half"] == "premium"
    # FVG движения вне Premium — контекстный допуск с аннотациями (§18)
    for lower in (10.0, 11.0, 13.0):
        e = by_lower[lower]
        assert e["type"] == "FVG"
        assert e["outside_premium"] is True
        assert e["half"] == "none"
        assert e["context"]["counter_swept"] is True
        assert e["context"]["htf_fvg50"] == {"zone_id": d1fvg_id, "tf": "D1"}

    # уведомление entries_ready содержит обе аннотации §18
    texts = render_ltf_messages(ready, _ctx(db, obs, sc))
    joined = "\n".join(texts)
    assert "вне Premium (допущено по контексту" in joined
    assert "Зона вне Premium — не критично для этого сценария" in joined
    assert "Контекст: обновление лоя = снятие SSL + тест 50% D1 FVG" in joined

    # idx26: касание допущенной FVG [10.0;10.5] — те же аннотации в touch
    _feed(db, engine, instrument_id, candles, 26)
    evs = _events(db, obs.id)
    assert evs[-1].kind == "touch"
    touch = evs[-1]
    assert (touch.payload["lower"], touch.payload["upper"]) == (10.0, 10.5)
    assert touch.payload["outside_premium"] is True
    assert touch.payload["half"] == "none"
    assert touch.payload["context"]["htf_fvg50"]["zone_id"] == d1fvg_id
    tt = "\n".join(render_ltf_messages(touch, _ctx(db, obs, sc)))
    assert "Зона вне Premium — не критично для этого сценария" in tt
    assert "Контекст: обновление лоя = снятие SSL + тест 50% D1 FVG" in tt

    # context_update отдельно не доставляется: ни группы, ни шаблона
    ctx_events = [e for e in evs if e.kind == "context_update"]
    assert all(
        render_ltf_messages(e, _ctx(db, obs, sc)) == [] for e in ctx_events
    )
    disp = LtfDispatcher(db, DetectorConfig(), sender=None)
    assert not any(disp._group_enabled(e.kind) for e in ctx_events)

    # replay не дублирует факты контекста и входы (§13/§11.5)
    before = len(db.list_ltf_events(observation_id=obs.id, limit=1000))
    res = engine.replay_observation(obs.id)
    assert res.events == []
    assert len(db.list_ltf_events(observation_id=obs.id, limit=1000)) == before


def test_fvg50_event_preferred_over_geometry(db: Database, instrument_id: int):
    """§18: из двух геометрически достигнутых D1 FVG выбирается зона с
    зафиксированным HTF-событием DEPTH_50/FVG_WEAKENED."""
    from app.engine.ltf.context import find_htf_fvg50_test
    from app.models import Event, EventKind

    plain = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=7.0, upper=8.5,
        formed_at=T0 - 8_000_000, confirmed_at=T0 - 7_000_000,
        status=ZoneStatus.ACTIVE,
    ))
    marked = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=6.0, upper=7.8,
        formed_at=T0 - 6_000_000, confirmed_at=T0 - 5_000_000,
        status=ZoneStatus.WEAKENED,
    ))
    db.insert_event(Event(
        id=None, zone_id=marked, cycle_id=1, kind=EventKind.FVG_WEAKENED,
        occurred_at=T0 - 1000, detected_at=T0 - 1000, price=6.9, depth=0.5,
    ))
    # экстремум 7.5 геометрически достигает 50% обеих зон (7.75 и 6.9... нет:
    # mid второй 6.9 < 7.5 — для bear достигнута только первая)
    hit = find_htf_fvg50_test(db, instrument_id, 7.5, Direction.BEAR)
    assert hit["zone_id"] == plain and hit["via"] == "geometry"
    # экстремум 6.8 достигает 50% обеих — приоритет зоне с событием
    hit2 = find_htf_fvg50_test(db, instrument_id, 6.8, Direction.BEAR)
    assert hit2["zone_id"] == marked and hit2["via"] == "event"
    # bull-сценарий зеркально: ищется медвежья D1 FVG выше
    assert find_htf_fvg50_test(db, instrument_id, 6.8, Direction.BULL) is None
