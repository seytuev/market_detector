"""§7/§8.5: диапазон Premium/Discount — связанные пары, версии, overlap (п.5–7)."""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import (
    PivotCandidate,
    current_range,
    eligible_overlap,
    level_in_half,
    range_recalc,
    target_half,
)
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfObservation, LtfRange, LtfScenario
from tests.conftest import H1_MS

T0 = 1_780_000_000_000


def mkp(
    kind: str, price: float, role: str, idx: int,
    confirmed_idx: int | None = None, pid: int | None = None,
) -> PivotCandidate:
    t = T0 + idx * H1_MS
    conf = T0 + (confirmed_idx if confirmed_idx is not None else idx + 3) * H1_MS
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=t, candle_open_time=t,
        confirmed_at=conf, left=3, right=3, role=role, pivot_id=pid,
    )


def test_bear_range_last_linked_pair():
    # последняя связанная пара LH→LL (§7): R_low=LL, R_high=LH
    ps = [mkp("high", 15, "LH", 1), mkp("low", 9, "LL", 2)]
    rng = current_range(ps, Direction.BEAR, T0 + 100 * H1_MS)
    assert rng is not None
    assert (rng.lower, rng.upper, rng.mid) == (9, 15, 12)
    # bear → Premium [M; R_high], bull → Discount [R_low; M]
    assert target_half(rng, Direction.BEAR) == (12, 15)
    assert target_half(rng, Direction.BULL) == (9, 12)


def test_bear_range_never_substitutes_global_hh():
    now = T0 + 100 * H1_MS
    # глобальный HH после пары не подменяет связанный LH
    ps = [mkp("high", 15, "LH", 1), mkp("low", 9, "LL", 2), mkp("high", 20, "HH", 3)]
    rng = current_range(ps, Direction.BEAR, now)
    assert (rng.lower, rng.upper) == (9, 15)
    # последний LL связан с HH (не LH) — пары нет, молча не подставляем
    ps = [mkp("high", 20, "HH", 1), mkp("low", 8, "LL", 2)]
    assert current_range(ps, Direction.BEAR, now) is None


def test_bull_range_last_linked_pair():
    now = T0 + 100 * H1_MS
    ps = [mkp("low", 10, "HL", 1), mkp("high", 15, "HH", 2)]
    rng = current_range(ps, Direction.BULL, now)
    assert (rng.lower, rng.upper, rng.mid) == (10, 15, 12.5)
    # последний HH связан с LL (не HL) — пары нет
    ps = [mkp("low", 8, "LL", 1), mkp("high", 15, "HH", 2)]
    assert current_range(ps, Direction.BULL, now) is None


def test_range_pending_and_invalid_geometry():
    now = T0 + 100 * H1_MS
    assert current_range([mkp("high", 15, "LH", 1)], Direction.BEAR, now) is None
    # R_high <= R_low → диапазон не строится (§7)
    ps = [mkp("high", 8, "LH", 1), mkp("low", 9, "LL", 2)]
    assert current_range(ps, Direction.BEAR, now) is None


def test_range_waits_three_right_candles():
    # новый LL до подтверждения (3 закрытые свечи справа) — кандидат,
    # действующий диапазон не меняется (§7, приёмка п.6)
    old_pair = [mkp("high", 15, "LH", 1), mkp("low", 9, "LL", 2)]
    new_pair = [mkp("high", 14, "LH", 6), mkp("low", 8, "LL", 7, confirmed_idx=10)]
    ps = old_pair + new_pair
    before = T0 + 9 * H1_MS + 1          # третья правая свеча ещё не закрылась
    after = T0 + 10 * H1_MS              # LL2 подтверждён
    rng = current_range(ps, Direction.BEAR, before)
    assert (rng.lower, rng.upper) == (9, 15)   # прежний диапазон
    rng = current_range(ps, Direction.BEAR, after)
    assert (rng.lower, rng.upper) == (8, 14)   # новая версия пары
    # range_recalc: None — без изменений; новый draft — после подтверждения
    assert range_recalc(rng, ps, Direction.BEAR, after) is None
    draft = range_recalc(
        current_range(ps, Direction.BEAR, before), ps, Direction.BEAR, after
    )
    assert draft is not None and (draft.lower, draft.upper) == (8, 14)


def _scenario(db: Database, instrument_id: int) -> LtfScenario:
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, activated_at=T0,
    ))
    return db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary",
    ))


def test_range_versions_persisted(db: Database, instrument_id: int):
    # приёмка п.7: новая опора создаёт range_version, старая геометрия сохраняется
    sc = _scenario(db, instrument_id)
    d1 = current_range(
        [mkp("high", 15, "LH", 1, pid=11), mkp("low", 9, "LL", 2, pid=12)],
        Direction.BEAR, T0 + 10 * H1_MS,
    )
    r1 = db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=d1.lower, upper=d1.upper,
        mid=d1.mid, anchor_low_pivot_id=d1.anchor_low_ref,
        anchor_high_pivot_id=d1.anchor_high_ref, available_at=d1.available_at,
    ))
    ps = [
        mkp("high", 15, "LH", 1, pid=11), mkp("low", 9, "LL", 2, pid=12),
        mkp("high", 14, "LH", 6, pid=13), mkp("low", 8, "LL", 7, pid=14),
    ]
    d2 = range_recalc(d1, ps, Direction.BEAR, T0 + 20 * H1_MS)
    assert d2 is not None
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=2, lower=d2.lower, upper=d2.upper,
        mid=d2.mid, anchor_low_pivot_id=d2.anchor_low_ref,
        anchor_high_pivot_id=d2.anchor_high_ref, available_at=d2.available_at,
        prev_version_id=r1,
    ))
    cur = db.get_current_ltf_range(sc.id)
    assert cur.version == 2 and (cur.lower, cur.upper) == (8, 14)
    versions = db.list_ltf_ranges(sc.id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[1].prev_version_id == r1
    # старая версия не переписана
    assert (versions[0].lower, versions[0].upper) == (9, 15)


def test_eligible_overlap_and_levels():
    # §8.5: частичное пересечение достаточно, границы не обрезаются
    assert eligible_overlap(98, 102, 100, 110)        # пример спеки: OB в Premium частично
    assert not eligible_overlap(90, 95, 100, 110)
    assert eligible_overlap(95, 100, 100, 110)        # равенство M — попадание
    assert eligible_overlap(110, 115, 100, 110)       # равенство R_high — попадание
    assert level_in_half(100, 100, 110)
    assert not level_in_half(99, 100, 110)
    assert level_in_half(110, 100, 110)


def test_update_range_no_version_without_anchor_change(db, cfg, instrument_id):
    """п.08/§9: повторная обработка тех же свечей и опор не создаёт версию
    диапазона (семантический дедуп в live-пути, как в delayed)."""
    from app.engine.ltf import LtfEngine, LtfTickResult

    engine = LtfEngine(db, cfg)
    sc = _scenario(db, instrument_id)
    ps = [mkp("high", 15, "LH", 1, pid=11), mkp("low", 9, "LL", 2, pid=12)]
    now = T0 + 10 * H1_MS
    res = LtfTickResult()
    r1 = engine._update_range(sc, [], now, res, avail=ps)
    assert r1 is not None and r1.version == 1
    # те же опоры и та же история повторно — версии нет
    assert engine._update_range(sc, [], now, res, avail=ps) is None
    assert [r.version for r in db.list_ltf_ranges(sc.id)] == [1]
    # новый подтверждённый LL при прежнем LH — смысловое изменение: версия
    ps2 = ps + [mkp("low", 8, "LL", 6, pid=13, confirmed_idx=9)]
    now2 = T0 + 12 * H1_MS
    r2 = engine._update_range(sc, [], now2, res, avail=ps2)
    assert r2 is not None and r2.version == 2
    assert (r2.lower, r2.upper) == (8, 15)
    # повтор новых опор — снова без версии
    assert engine._update_range(sc, [], now2, res, avail=ps2) is None
    assert [r.version for r in db.list_ltf_ranges(sc.id)] == [1, 2]


# --- ТЗ «LTF Current Setup» §3/§9: опоры — ровно 3 правые, якоря не NULL ---

_PIVOT_BARS = [
    (10, 10.5, 9.5, 10),
    (11, 11.5, 10.5, 11),
    (12, 12.5, 11.5, 12),
    (13, 15.0, 12.5, 14),   # pivot high 15.0 (idx 3)
    (12, 12.5, 11.0, 11.5),
    (11, 11.5, 10.0, 10.5),
    (10, 10.5, 9.0, 9.5),
]


def test_range_pivots_fixed_three_right_with_zero_setting(db, instrument_id):
    """ТЗ §3/§9: опоры диапазона подтверждаются РОВНО тремя правыми
    закрытыми свечами; ltf_range_right=0 (битое значение settings.json)
    на расчёт не влияет. Транзитный путь (структурный профиль ≠ 3/3)."""
    from app.config import DetectorConfig
    from app.engine.ltf import LtfEngine
    from tests.conftest import make_h1_candles

    candles = make_h1_candles(_PIVOT_BARS, T0, instrument_id)
    for right in (0, 3):  # 0 — продакшн-баг; поведение обязано совпадать с 3
        cfg = DetectorConfig(
            ltf_range_right=right, ltf_structure_left=5, ltf_structure_right=5,
        )
        engine = LtfEngine(db, cfg)
        # 1–2 правые свечи — pivot ещё НЕ подтверждён (не 0!)
        for n in (5, 6):
            up_to = candles[:n]
            ps = engine._range_pivots(
                instrument_id, up_to, up_to[-1].close_time
            )
            assert all(p.price != 15.0 for p in ps), f"right={right}, n={n}"
        # третья правая закрылась — подтверждён
        ps = engine._range_pivots(
            instrument_id, candles, candles[-1].close_time
        )
        pivot = next(p for p in ps if p.price == 15.0)
        assert pivot.kind == "high" and pivot.state == "confirmed", right


def test_range_anchor_ids_persisted_with_zero_setting(db, instrument_id):
    """Продакшн-корень тысяч версий: ltf_range_right=0 → транзитные опоры
    без id → anchor_*_pivot_id=NULL → дедуп версий не срабатывал.
    Теперь якоря персистятся (id ltf_pivot), повторная обработка тех же
    свечей версию не создаёт, версия — только при смене опор."""
    from app.config import DetectorConfig
    from app.engine.ltf import LtfEngine, LtfTickResult
    from app.models_ltf import LtfPivot

    cfg = DetectorConfig(ltf_range_right=0)  # структурный профиль 3/3
    engine = LtfEngine(db, cfg)
    sc = _scenario(db, instrument_id)
    p_high = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=15.0, kind="high",
        pivot_at=T0 + H1_MS, candle_open_time=T0 + H1_MS,
        confirmed_at=T0 + 4 * H1_MS, role="LH", state="confirmed",
    ))
    p_low = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=9.0, kind="low",
        pivot_at=T0 + 2 * H1_MS, candle_open_time=T0 + 2 * H1_MS,
        confirmed_at=T0 + 5 * H1_MS, role="LL", state="confirmed",
    ))
    now = T0 + 10 * H1_MS
    res = LtfTickResult()
    r1 = engine._update_range(sc, [], now, res)  # avail=None → опоры из БД
    assert r1 is not None and r1.version == 1
    # якоря персистятся — НЕ NULL (дедуп по (lower, upper, anchor ids))
    assert r1.anchor_low_pivot_id == p_low
    assert r1.anchor_high_pivot_id == p_high
    # повторная обработка тех же данных — версии нет
    assert engine._update_range(sc, [], now, res) is None
    assert [r.version for r in db.list_ltf_ranges(sc.id)] == [1]
    # смена опоры — версия создаётся
    p_low2 = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=8.0, kind="low",
        pivot_at=T0 + 6 * H1_MS, candle_open_time=T0 + 6 * H1_MS,
        confirmed_at=T0 + 9 * H1_MS, role="LL", state="confirmed",
    ))
    r2 = engine._update_range(sc, [], T0 + 12 * H1_MS, res)
    assert r2 is not None and r2.version == 2
    assert r2.anchor_low_pivot_id == p_low2
    assert [r.version for r in db.list_ltf_ranges(sc.id)] == [1, 2]


def test_transit_range_pivots_resolve_db_anchor_ids(db, cfg, instrument_id):
    """Транзитный расчёт 3/3 (структурный профиль ≠ 3/3) разрешает якоря
    по материализованным pivots БД — anchor ids персистятся и здесь."""
    from app.config import DetectorConfig
    from app.engine.ltf import LtfEngine
    from app.models_ltf import LtfPivot
    from tests.conftest import make_h1_candles

    candles = make_h1_candles(_PIVOT_BARS, T0, instrument_id)
    pid = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=15.0, kind="high",
        pivot_at=candles[3].open_time, candle_open_time=candles[3].open_time,
        confirmed_at=candles[6].close_time, role="LH", state="confirmed",
    ))
    engine = LtfEngine(db, DetectorConfig(
        ltf_range_right=0, ltf_structure_left=5, ltf_structure_right=5,
    ))
    ps = engine._range_pivots(instrument_id, candles, candles[-1].close_time)
    pivot = next(p for p in ps if p.price == 15.0)
    assert pivot.pivot_id == pid
