"""§7 (Этап 4): origin_reversal_range — диапазон от якоря-источника причинного
движения первичного слома до первой валидной continuation-пары.

Покрытие: границы/mid/kind/anchor_policy, подтверждение обоих якорей тремя
правыми свечами (иначе range_pending), переход origin_reversal → continuation
новой версией (старая — история), политика continuation_only, идемпотентность
replay, стартовый якорь — из движения (не «максимальный хай окна»).

Перманентность снятого BSL при смене версии уже покрыта:
tests/test_ltf_engine.py::test_swept_bsl_not_resurrected_on_range_update (п.17)
и tests/test_final_eligibility.py::test_swept_level_not_revived_by_new_range_version.
"""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.pivots import PivotCandidate
from app.engine.ltf.ranges import origin_reversal_range, range_recalc
from app.models import Direction
from tests.test_ltf_breaks import _series
from tests.test_ltf_engine import (
    SERIES_H_CLOSES,
    SERIES_H_HL,
    T0,
    _events,
    _feed,
    _setup,
)

H1 = 3_600_000


def _candles(instrument_id: int):
    return _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)


def _p(pid: int, price: float, kind: str, pivot_at: int,
       confirmed_at: int, role: str) -> PivotCandidate:
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=pivot_at,
        candle_open_time=pivot_at, confirmed_at=confirmed_at,
        left=3, right=3, state="confirmed", pivot_id=pid, role=role,
    )


# --------------------------------------------------------------------- #
# Чистая функция origin_reversal_range
# --------------------------------------------------------------------- #

def test_start_anchor_is_movement_start_not_earlier_higher_high():
    """Якорь-источник — start-pivot движения; более ранний и более высокий
    хай окна НЕ подставляется. Конец — минимальный подтверждённый low движения."""
    pivots = [
        _p(1, 20.0, "high", 0, 3 * H1, "HH"),      # более ранний высокий хай
        _p(2, 15.0, "high", 10 * H1, 13 * H1, "HH"),  # start движения
        _p(3, 9.0, "low", 20 * H1, 23 * H1, "LL"),
        _p(4, 8.0, "low", 30 * H1, 33 * H1, "LL"),    # минимум движения
    ]
    d = origin_reversal_range(pivots, 2, Direction.BEAR, now_ms=40 * H1)
    assert d is not None
    assert (d.lower, d.upper, d.mid) == (8.0, 15.0, 11.5)
    assert d.anchor_high_ref == 2 and d.anchor_low_ref == 4
    assert d.kind == "origin_reversal"
    assert d.available_at == 33 * H1            # подтверждение младшей опоры
    assert d.evidence["anchor_role"] == "HH"    # роль HH не переименована


def test_both_anchors_require_confirmation():
    """Неподтверждённый конец (нет 3 правых свечей) → старший подтверждённый
    экстремум; нет подтверждённого конца вообще → None (range_pending)."""
    pivots = [
        _p(2, 15.0, "high", 10 * H1, 13 * H1, "HH"),
        _p(3, 9.0, "low", 20 * H1, 23 * H1, "LL"),
        _p(4, 8.0, "low", 30 * H1, 33 * H1, "LL"),
    ]
    d = origin_reversal_range(pivots, 2, Direction.BEAR, now_ms=25 * H1)
    assert d is not None and (d.lower, d.upper) == (9.0, 15.0)
    # минимум 8.0 ещё не подтверждён, а 9.0 — тоже нет → диапазона нет
    assert origin_reversal_range(pivots, 2, Direction.BEAR,
                                 now_ms=13 * H1) is None
    # start не подтверждён / не найден → None
    assert origin_reversal_range(pivots, 2, Direction.BEAR,
                                 now_ms=12 * H1) is None
    assert origin_reversal_range(pivots, 999, Direction.BEAR,
                                 now_ms=40 * H1) is None


def test_bull_mirror():
    pivots = [
        _p(1, 10.0, "low", 10 * H1, 13 * H1, "LL"),   # start движения
        _p(2, 12.0, "high", 20 * H1, 23 * H1, "HH"),
        _p(3, 13.0, "high", 30 * H1, 33 * H1, "HH"),  # максимум движения
    ]
    d = origin_reversal_range(pivots, 1, Direction.BULL, now_ms=40 * H1)
    assert d is not None
    assert (d.lower, d.upper) == (10.0, 13.0)
    assert d.anchor_low_ref == 1 and d.anchor_high_ref == 3
    assert d.kind == "origin_reversal"


def test_invalid_geometry_and_no_continuation_backslide():
    """R_high <= R_low → None; обратный переход continuation → origin_reversal
    запрещён (prev continuation, пары нет → range_pending, не origin)."""
    pivots = [
        _p(2, 15.0, "high", 10 * H1, 13 * H1, "HH"),
        _p(3, 16.0, "low", 20 * H1, 23 * H1, "LL"),   # выше start — невалидно
    ]
    assert origin_reversal_range(pivots, 2, Direction.BEAR,
                                 now_ms=40 * H1) is None
    prev = origin_reversal_range(
        [_p(2, 15.0, "high", 10 * H1, 13 * H1, "HH"),
         _p(3, 9.0, "low", 20 * H1, 23 * H1, "LL")],
        2, Direction.BEAR, now_ms=40 * H1,
    )
    assert prev is not None
    prev_cont = range_recalc(
        prev, [
            _p(2, 15.0, "high", 10 * H1, 13 * H1, "HH"),
            _p(3, 9.0, "low", 20 * H1, 23 * H1, "LL"),
            # валидная continuation-пара: LH 12.0 → LL 8.0
            _p(4, 12.0, "high", 30 * H1, 33 * H1, "LH"),
            _p(5, 8.0, "low", 40 * H1, 43 * H1, "LL"),
        ],
        Direction.BEAR, now_ms=50 * H1,
        origin_start_ref=2, anchor_policy="origin_reversal",
    )
    assert prev_cont is not None and prev_cont.kind == "continuation"
    assert (prev_cont.lower, prev_cont.upper) == (8.0, 12.0)
    # continuation → origin_reversal: никогда
    assert range_recalc(
        prev_cont, [
            _p(2, 15.0, "high", 10 * H1, 13 * H1, "HH"),
            _p(3, 9.0, "low", 20 * H1, 23 * H1, "LL"),
        ],
        Direction.BEAR, now_ms=50 * H1,
        origin_start_ref=2, anchor_policy="origin_reversal",
    ) is None


# --------------------------------------------------------------------- #
# Движок: серия H (первичный bear BOS разворота)
# --------------------------------------------------------------------- #

def test_origin_range_lifecycle_after_primary_bos(db: Database, cfg,
                                                  instrument_id: int):
    """Эталонный кейс §7: после первичного bear BOS рабочее движение начинается
    от HH прежней структуры — диапазон от этого якоря до continuation-пары."""
    engine = LtfEngine(db, cfg)
    candles = _candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)

    # idx14: BOS есть, но конец движения (low idx14) не подтверждён тремя
    # правыми свечами → честный range_pending, предварительного диапазона нет
    _feed(db, engine, instrument_id, candles, 14)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state == "range_pending"
    assert db.get_current_ltf_range(sc.id) is None
    bos = [e for e in _events(db, obs.id) if e.kind == "bos"][0]
    assert bos.payload["range_pending"] is True

    # idx17: low idx14 подтверждён (3 правые закрытые свечи) → v1 origin
    _feed(db, engine, instrument_id, candles, 17)
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "monitoring_entries"
    rng = db.get_current_ltf_range(sc.id)
    assert rng is not None
    assert (rng.version, rng.kind) == (1, "origin_reversal")
    assert (rng.lower, rng.upper, rng.mid) == (7.8, 15.0, 11.4)
    assert rng.anchor_policy == "origin_reversal"
    assert rng.available_at == candles[17].close_time
    high = db.get_ltf_pivot(rng.anchor_high_pivot_id)
    low = db.get_ltf_pivot(rng.anchor_low_pivot_id)
    assert (high.price, high.role) == (15.0, "HH")   # HH не переименован в LH
    assert low.price == 7.8
    # start-якорь — именно start-pivot причинного движения сценария
    mv = db.get_ltf_movement(sc.origin_movement_id)
    assert rng.anchor_high_pivot_id == mv.start_pivot_id

    # idx23: валидная пара LH 9.5 → LL 7.6 → v2 continuation; v1 — история
    _feed(db, engine, instrument_id, candles, 23)
    rng = db.get_current_ltf_range(sc.id)
    assert (rng.version, rng.kind) == (2, "continuation")
    assert (rng.lower, rng.upper) == (7.6, 9.5)
    history = db.list_ltf_ranges(sc.id)
    assert [(r.version, r.kind) for r in history] == [
        (1, "origin_reversal"), (2, "continuation"),
    ]
    assert (history[0].lower, history[0].upper) == (7.8, 15.0)

    # replay не создаёт новых версий и не переписывает историю (§8/§13)
    engine.replay_observation(obs.id)
    assert [(r.version, r.kind) for r in db.list_ltf_ranges(sc.id)] == [
        (1, "origin_reversal"), (2, "continuation"),
    ]


def test_continuation_only_policy_keeps_range_pending(db: Database, cfg,
                                                      instrument_id: int):
    """Политика continuation_only: без валидной пары — честный range_pending,
    диапазон от origin-якоря не строится (прежнее поведение)."""
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)

    _feed(db, engine, instrument_id, candles, 17)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state == "range_pending"
    assert db.get_current_ltf_range(sc.id) is None

    _feed(db, engine, instrument_id, candles, 23)
    rng = db.get_current_ltf_range(sc.id)
    assert rng is not None
    assert (rng.version, rng.kind) == (1, "continuation")
    assert (rng.lower, rng.upper) == (7.6, 9.5)
    assert rng.anchor_policy == "continuation_only"
