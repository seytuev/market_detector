"""Этап 8: плато равных экстремумов и ambiguous-свечи (правила v1 §10.2).

R43: несколько равных в тиках high/low — единая подтверждаемая опора плато
(уровень, начало/конец, подтверждение после правого окна последнего
участника; расширение сдвигает подтверждение, без заднего числа). Тик-
равенство без процентов. R44: both_sides-свеча исключена из опор, OHLC
сохранён и участвует в касаниях. Интеграция: BOS/SMS от плато-уровня.
"""
from __future__ import annotations

from dataclasses import replace

from app.db import Database
from app.engine.ltf.breaks import detect_breaks
from app.engine.ltf.engine import LtfEngine
from app.engine.ltf.pivots import confirmed_pivots, find_h1_pivots, find_plateaus, same_tick
from app.engine.ltf.structure import StructureBatch
from app.models import Direction, Zone, ZoneStatus, ZoneType
from tests.conftest import H1_MS, make_h1_candles

T = 1_780_012_800_000
H = H1_MS
TICK = 0.5


def _series(highs, lows=None, start=T, iid=1):
    lows = lows or [h - 1.5 for h in highs]
    return make_h1_candles(
        [(h - 1.0, h, l, h - 0.5) for h, l in zip(highs, lows)], start,
        instrument_id=iid,
    )


# --------------------------------------------------------------------- #
# Детекция плато (R43)
# --------------------------------------------------------------------- #

def test_r43_equal_highs_form_single_plateau():
    # high=12 на трёх подряд свечах, окно строго ниже — единая опора плато
    candles = _series([10, 11, 12, 12, 12, 10, 9, 8, 7])
    found = find_plateaus(candles, 3, 3, TICK)
    assert len(found) == 1
    p = found[0]
    assert p.kind == "high" and p.price == 12
    assert p.evidence["start_open"] == candles[2].open_time
    assert p.evidence["end_open"] == candles[4].open_time
    assert p.pivot_at == candles[4].open_time
    # подтверждение — закрытие (последний участник idx4 + right=3) = close idx7
    assert p.confirmed_at == candles[7].close_time
    assert p.evidence["revision"] == 2
    assert len(p.evidence["members"]) == 3
    assert p.state == "confirmed"


def test_r43_low_plateau_mirror():
    candles = _series([10, 11, 12, 11.5, 10.5, 12, 13, 14],
                      lows=[9, 8, 7, 7, 7, 8.5, 9.5, 10.5])
    found = [p for p in find_plateaus(candles, 3, 3, TICK) if p.kind == "low"]
    assert len(found) == 1
    assert found[0].price == 7
    assert found[0].evidence["start_open"] == candles[2].open_time
    assert found[0].evidence["end_open"] == candles[4].open_time


def test_tick_equality_whole_steps_only():
    assert same_tick(12.0, 12.4, 0.5) is True     # различие меньше тика
    assert same_tick(12.0, 12.5, 0.5) is False    # ровно тик — уже не равны
    assert same_tick(12.0, 13.0, 0.5) is False
    assert same_tick(12.0, 12.4, 0.0) is False    # tick=0 — плато выключены
    # различие в тик — две отдельные свечи, плато нет
    candles = _series([10, 11, 12.0, 12.5, 10, 9, 8, 7])
    assert find_plateaus(candles, 3, 3, TICK) == []


def test_single_candle_is_not_plateau():
    candles = _series([10, 11, 12, 10, 9, 8, 7, 6])
    assert find_plateaus(candles, 3, 3, TICK) == []


def test_no_plateau_without_full_right_window():
    # до правого окна последнего участника плато не существует (без будущего)
    candles = _series([10, 11, 12, 12, 12, 10, 9, 8, 7])
    assert find_plateaus(candles[:5], 3, 3, TICK) == []
    assert find_plateaus(candles[:7], 3, 3, TICK) == []
    assert len(find_plateaus(candles[:8], 3, 3, TICK)) == 1


def test_extension_moves_confirmation_forward():
    base = _series([10, 11, 12, 12, 10, 9, 8, 7])
    extended = _series([10, 11, 12, 12, 12, 9, 8, 7, 6])
    rev1 = find_plateaus(base, 3, 3, TICK)[0]
    rev2 = find_plateaus(extended, 3, 3, TICK)[0]
    assert rev1.evidence["end_open"] == base[3].open_time
    assert rev2.evidence["end_open"] == extended[4].open_time
    # расширенная группа подтверждается от нового последнего участника —
    # позже, не задним числом
    assert rev2.confirmed_at > rev1.confirmed_at
    assert rev2.evidence["revision"] == rev1.evidence["revision"] + 1


def test_both_sides_plateau_range_is_ambiguous():
    # один диапазон (idx0-2) — и high-, и low-плато: порядок не выдумываем
    candles = make_h1_candles(
        [(11, 12, 8, 11), (11.5, 12, 8, 11), (10.5, 12, 8, 10.5),
         (10, 11, 8.5, 10), (9, 10, 9.5, 9.5), (8, 9, 8.5, 8.5),
         (9, 10, 10, 9.5), (10, 11, 11, 10.5)], T,
    )
    found = find_plateaus(candles, 3, 3, TICK)
    assert len(found) == 2
    assert {p.kind for p in found} == {"high", "low"}
    assert all(p.state == "ambiguous" for p in found)
    assert confirmed_pivots(found, T + 30 * H1_MS) == []


# --------------------------------------------------------------------- #
# Инкремент (StructureBatch): ревизия заменяет опору без заднего числа
# --------------------------------------------------------------------- #

def test_batch_revision_via_reset_keeps_late_confirmation():
    prefix = _series([10, 11, 12, 12, 10, 9, 8, 7])
    batch = StructureBatch()
    batch.pivots_for(prefix, 3, 3, TICK)
    avail1 = batch.avail_for(prefix[-1].close_time)
    plateau1 = [p for p in avail1 if p.evidence.get("plateau")]
    assert len(plateau1) == 1
    assert plateau1[0].pivot_at == prefix[3].open_time
    rev1_confirmed = plateau1[0].confirmed_at
    # «расширение» группы — исправление свечи внутри серии: префикс изменён,
    # пакет пересчитывается полностью (F10), новая форма — с ПОЗДНИМ
    # подтверждением (не задним числом)
    revised = list(prefix)
    revised[4] = replace(prefix[4], high=12)
    revised[7] = replace(prefix[7], low=6.0)
    extended = revised + _series([6], start=prefix[-1].open_time + H1_MS)
    batch.pivots_for(extended, 3, 3, TICK)
    avail2 = batch.avail_for(extended[-1].close_time)
    plateau2 = [p for p in avail2 if p.evidence.get("plateau")]
    assert len(plateau2) == 1
    assert plateau2[0].pivot_at == extended[4].open_time
    assert plateau2[0].confirmed_at == extended[7].close_time
    assert plateau2[0].confirmed_at > rev1_confirmed
    assert plateau2[0].evidence["revision"] == plateau1[0].evidence["revision"] + 1


# --------------------------------------------------------------------- #
# R44: both_sides-свеча исключена из опор, OHLC сохранён
# --------------------------------------------------------------------- #

def test_r44_both_sides_excluded_ohlc_kept(db, cfg, instrument_id):
    # outside-свеча (idx3) — одновременно high- и low-pivot: ambiguous,
    # в опорах не участвует; сама свеча остаётся в истории и в касаниях
    candles = make_h1_candles([
        (10, 11, 9.5, 10), (10.5, 11.5, 10, 11), (11, 12, 10.5, 11.5),
        (10, 13, 8, 12),                        # outside: и high, и low
        (9, 10, 8.5, 9.5), (9.5, 10.5, 9, 10), (10, 11, 9.5, 10.5),
    ], T, instrument_id=instrument_id)
    db.insert_candles(candles)
    cands = find_h1_pivots(candles, 3, 3)
    both = [p for p in cands if p.state == "ambiguous"]
    assert both and all(p.pivot_at == candles[3].open_time for p in both)
    avail = confirmed_pivots(cands, candles[-1].close_time)
    assert all(p.pivot_at != candles[3].open_time for p in avail)
    # OHLC свечи в БД и доступна касаниям
    stored = db.get_candles(instrument_id, "H1")
    assert len(stored) == 7 and stored[3].open_time == candles[3].open_time
    from app.engine.scanner import Scanner
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=9.0, upper=9.5,
        formed_at=T - H, confirmed_at=T - H + 1, status=ZoneStatus.ACTIVE,
    ))
    ev = Scanner(db, cfg).on_price(instrument_id, 9.2, candles[3].close_time)
    assert ev and ev[0].zone_id == zone_id


# --------------------------------------------------------------------- #
# Интеграция: плато-уровень — структурная опора для BOS/SMS и хранение
# --------------------------------------------------------------------- #

def test_plateau_level_arms_break():
    # плато high 15 (idx5-6) становится опорным LH — bull BOS пробивает
    # плато-уровень 15 на idx12 (интеграция с roles/breaks)
    from tests.test_ltf_breaks import SERIES_D_CLOSES, SERIES_D_HL, _series
    hl = list(SERIES_D_HL)
    hl[5] = (15.0, hl[5][1])
    candles = _series(hl, SERIES_D_CLOSES)
    pivots = find_h1_pivots(candles, 3, 3, TICK)
    plateaus = [p for p in pivots if p.evidence.get("plateau") and p.price == 15.0]
    assert len(plateaus) == 1
    avail = confirmed_pivots(pivots, candles[15].close_time)
    from app.engine.ltf.roles import assign_roles
    assigned = assign_roles(avail)
    for i, p in enumerate(avail):
        p.role = assigned.roles[i]
    res = detect_breaks(avail, candles, Direction.BULL, candles[15].close_time)
    assert any(
        e.kind in ("BOS", "SMS") and e.break_level == 15.0 for e in res.events
    )


def test_plateau_persisted_on_engine_sync(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series([10, 11, 12, 12, 12, 10, 9, 8, 7], iid=instrument_id)
    db.insert_candles(candles)
    engine.process_h1_close(instrument_id, now_ms=candles[-1].close_time)
    rows = db.list_ltf_plateau(instrument_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["side"] == "high" and row["level"] == 12
    assert row["start_open"] == candles[2].open_time
    assert row["end_open"] == candles[4].open_time
    assert row["confirmed_at"] == candles[7].close_time
    assert row["revision"] == 2
    # опорная запись для downstream есть и в ltf_pivot
    pivots = [p for p in db.list_ltf_pivots(instrument_id)
              if p.price == 12 and p.kind == "high"]
    assert pivots and pivots[0].pivot_at == candles[4].open_time
