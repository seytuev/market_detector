"""ТЗ «Единый движок HTF/LTF» §5: внутренние уровни ликвидности после теста OB.

Экстремум самостоятельного теста → кандидат; подтверждение — строгое 3+3 на
ТФ уровня только после закрытия i+3; пересчёт при углублении до подтверждения;
снятие не отменяет родительский OB; экстремум вне [lower, upper] уровня не
образует; D1- и H1-экстремумы — раздельные записи.
"""
from __future__ import annotations

from app.engine.scanner import Scanner
from app.models import (
    Direction,
    TIMEFRAME_MINUTES,
    Visit,
    Zone,
    ZoneStatus,
    ZoneType,
)

from .conftest import make_candle, make_h1_candles

T0 = 1780272000000
D1_MS = TIMEFRAME_MINUTES["D1"] * 60_000
H1_MS = TIMEFRAME_MINUTES["H1"] * 60_000


def _day(i: int) -> int:
    return T0 + i * D1_MS


def _ob(db, instrument_id, direction=Direction.BULL, lower=100.0, upper=110.0,
        timeframe="D1"):
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB, direction=direction,
        timeframe=timeframe, lower=lower, upper=upper, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.ACTIVE,
    ))


def _visit(db, zid, entered_at, exited_at, extreme, max_depth=0.4):
    """Закрытый визит (exit_kind='return') с заданным экстремумом захода."""
    vid = db.open_visit(Visit(
        id=None, zone_id=zid, cycle_id=1, entered_at=entered_at,
        max_depth=max_depth, observed=False, extreme=extreme, d_raw=max_depth,
    ))
    db.close_visit(vid, exited_at, max_depth, exit_kind="return", extreme=extreme)
    return vid


def _d1_candles(lows: dict[int, float], days: int, base_low=108.0,
                high=111.0, instrument_id=1):
    """D1-свечи days штук от T0; lows — переопределения low по номеру дня."""
    return [
        make_candle(_day(i), 109.0, high, lows.get(i, base_low), 109.5, "D1",
                    instrument_id=instrument_id)
        for i in range(days)
    ]


def test_candidate_confirms_only_after_three_right_candles(db, cfg, instrument_id):
    """Приёмка H: уровень активен только после трёх правых закрытых свечей;
    до этого — candidate с confirmed_at NULL (без ложного исторического
    подтверждения)."""
    zid = _ob(db, instrument_id)
    vid = _visit(db, zid, _day(5), _day(7) + D1_MS - 1, extreme=104.0)
    scanner = Scanner(db, cfg)
    candles = _d1_candles({6: 104.0}, days=10, instrument_id=instrument_id)
    for c in candles[:9]:  # дни 0..8: справа от pivot (день 6) только 2 свечи
        scanner.on_closed_candle(c)
    levels = db.list_inner_levels(parent_ob_id=zid)
    assert len(levels) == 1
    lv = levels[0]
    assert lv.kind == "ssl"                    # bull OB → минимум теста
    assert lv.price == 104.0
    assert lv.pivot_time == _day(6)
    assert lv.source_test_id == vid
    assert lv.status == "candidate"
    assert lv.confirmed_at is None

    scanner.on_closed_candle(candles[9])       # третья правая свеча закрылась
    lv = db.list_inner_levels(parent_ob_id=zid)[0]
    assert lv.status == "active"
    assert lv.confirmed_at == _day(9) + D1_MS  # закрытие i+3


def test_candidate_recalculated_on_deeper_test(db, cfg, instrument_id):
    """Новый более глубокий тест до подтверждения пересчитывает кандидата:
    новая цена/pivot_time/source_test_id, старый кандидат активным не стал."""
    zid = _ob(db, instrument_id)
    v1 = _visit(db, zid, _day(2), _day(4) + D1_MS - 1, extreme=105.0)
    scanner = Scanner(db, cfg)
    candles = _d1_candles({3: 105.0, 6: 103.0}, days=10, instrument_id=instrument_id)
    for c in candles[:6]:  # дни 0..5: кандидат 105 на дне 3, ещё не подтверждён
        scanner.on_closed_candle(c)
    lv = db.list_inner_levels(parent_ob_id=zid)[0]
    assert (lv.price, lv.pivot_time, lv.source_test_id) == (105.0, _day(3), v1)
    assert lv.status == "candidate"

    v2 = _visit(db, zid, _day(5), _day(7) + D1_MS - 1, extreme=103.0)
    for c in candles[6:8]:  # дни 6..7: пересчёт на более глубокий экстремум
        scanner.on_closed_candle(c)
    levels = db.list_inner_levels(parent_ob_id=zid)
    assert len(levels) == 1                    # пересчёт, а не второй кандидат
    lv = levels[0]
    assert (lv.price, lv.pivot_time, lv.source_test_id) == (103.0, _day(6), v2)
    assert lv.status == "candidate"
    assert lv.confirmed_at is None             # часы подтверждения пошли заново

    for c in candles[8:]:
        scanner.on_closed_candle(c)
    lv = db.list_inner_levels(parent_ob_id=zid)[0]
    assert lv.status == "active"
    assert lv.price == 103.0                   # старый экстремум не подтверждён


def test_taken_level_keeps_parent_and_others(db, cfg, instrument_id):
    """Приёмка I: снятие одного уровня не отменяет родительский OB и другие
    уровни; снятый остаётся в истории (status='taken', taken_at)."""
    zid = _ob(db, instrument_id)
    scanner = Scanner(db, cfg)
    candles = _d1_candles({3: 105.0, 7: 106.0}, days=12, instrument_id=instrument_id)
    _visit(db, zid, _day(2), _day(4) + D1_MS - 1, extreme=105.0)
    for c in candles[:7]:  # уровень 105 (pivot день 3) подтверждён на дне 6
        scanner.on_closed_candle(c)
    _visit(db, zid, _day(6), _day(8) + D1_MS - 1, extreme=106.0)
    for c in candles[7:11]:  # второй уровень 106 (pivot день 7) — свой кандидат
        scanner.on_closed_candle(c)
    levels = db.list_inner_levels(parent_ob_id=zid)
    assert len(levels) == 2                    # confirmed раньше → новый тест дал новый
    by_price = {lv.price: lv for lv in levels}
    assert by_price[105.0].status == "active"
    assert by_price[106.0].status == "active"

    before = db.get_zone(zid)
    # свеча снимает только верхний SSL (106): 105 < low=105.5 < 106
    scanner.on_closed_candle(
        make_candle(_day(11), 109.0, 111.0, 105.5, 109.0, "D1",
                    instrument_id=instrument_id)
    )
    taken = db.get_inner_level_by_key(zid, "D1", "ssl", 106.0, _day(7))
    alive = db.get_inner_level_by_key(zid, "D1", "ssl", 105.0, _day(3))
    assert taken.status == "taken"
    assert taken.taken_at == _day(11) + D1_MS
    assert alive.status == "active"            # другой уровень не затронут
    after = db.get_zone(zid)
    assert after.status == before.status       # родительский OB не изменился
    assert after.market_validity == "active"
    # снятый остаётся в истории
    history = db.list_inner_levels(parent_ob_id=zid)
    assert any(lv.status == "taken" for lv in history)
    # повторное снятие не двигает taken_at назад
    scanner.on_closed_candle(
        make_candle(_day(12), 109.0, 111.0, 104.0, 109.0, "D1",
                    instrument_id=instrument_id)
    )
    assert db.get_inner_level_by_key(zid, "D1", "ssl", 106.0, _day(7)).taken_at == \
        _day(11) + D1_MS
    assert db.get_inner_level_by_key(zid, "D1", "ssl", 105.0, _day(3)).status == "taken"


def test_extreme_outside_ob_range_creates_no_level(db, cfg, instrument_id):
    """Экстремум теста вне диапазона OB [lower, upper] — уровень не создаётся."""
    zid = _ob(db, instrument_id)
    _visit(db, zid, _day(2), _day(4) + D1_MS - 1, extreme=95.0)  # ниже lower=100
    scanner = Scanner(db, cfg)
    for c in _d1_candles({3: 95.0}, days=8, instrument_id=instrument_id):
        scanner.on_closed_candle(c)
    assert db.list_inner_levels(parent_ob_id=zid) == []


def test_bear_ob_creates_bsl(db, cfg, instrument_id):
    """Bear OB → внутренний BSL на максимуме теста."""
    zid = _ob(db, instrument_id, direction=Direction.BEAR)
    _visit(db, zid, _day(2), _day(4) + D1_MS - 1, extreme=106.0)
    scanner = Scanner(db, cfg)
    candles = [
        make_candle(_day(i), 105.0, 106.0 if i == 3 else 104.0, 101.0, 102.0,
                    "D1", instrument_id=instrument_id)
        for i in range(8)
    ]
    for c in candles:
        scanner.on_closed_candle(c)
    lv = db.list_inner_levels(parent_ob_id=zid)[0]
    assert lv.kind == "bsl"
    assert lv.price == 106.0
    assert lv.pivot_time == _day(3)
    assert lv.status == "active"


def test_idempotent_replay(db, cfg, instrument_id):
    """Повторный прогон тех же свечей не дублирует записи и не двигает
    confirmed_at/taken_at."""
    zid = _ob(db, instrument_id)
    _visit(db, zid, _day(5), _day(7) + D1_MS - 1, extreme=104.0)
    scanner = Scanner(db, cfg)
    candles = _d1_candles({6: 104.0}, days=11, instrument_id=instrument_id)
    for c in candles:
        scanner.on_closed_candle(c)
    snap = [
        (lv.kind, lv.price, lv.pivot_time, lv.confirmed_at, lv.status, lv.taken_at)
        for lv in db.list_inner_levels(parent_ob_id=zid)
    ]
    for c in candles:  # полный повторный replay
        scanner.on_closed_candle(c)
    snap2 = [
        (lv.kind, lv.price, lv.pivot_time, lv.confirmed_at, lv.status, lv.taken_at)
        for lv in db.list_inner_levels(parent_ob_id=zid)
    ]
    assert snap == snap2


def test_d1_and_h1_levels_are_separate(db, cfg, instrument_id):
    """Внутри D1 OB D1- и H1-экстремумы — раздельные записи inner_level:
    H1-уровень считается по H1-свечам, когда они проходят через scanner."""
    zid = _ob(db, instrument_id, timeframe="D1")
    # H1-свечи дня 6: минимум 103.5 на часе 3 (он же — low D1-свечи дня 6)
    h1 = make_h1_candles(
        [(107.5, 108.0, 107.0, 107.5)] * 3
        + [(104.0, 107.0, 103.5, 106.0)]
        + [(107.5, 108.0, 107.0, 107.5)] * 3,
        start_ms=_day(6), instrument_id=instrument_id,
    )
    _visit(db, zid, _day(5), _day(7) + D1_MS - 1, extreme=103.5)
    scanner = Scanner(db, cfg)
    for c in _d1_candles({6: 103.5}, days=10, instrument_id=instrument_id):
        scanner.on_closed_candle(c)
    for c in h1:
        scanner.on_closed_candle(c)
    levels = db.list_inner_levels(parent_ob_id=zid)
    tfs = sorted(lv.timeframe for lv in levels)
    assert tfs == ["D1", "H1"]
    d1 = next(lv for lv in levels if lv.timeframe == "D1")
    h1lv = next(lv for lv in levels if lv.timeframe == "H1")
    assert d1.pivot_time == _day(6)
    assert h1lv.pivot_time == _day(6) + 3 * H1_MS
    assert d1.status == "active"
    assert h1lv.status == "active"
    assert h1lv.confirmed_at == _day(6) + 7 * H1_MS  # закрытие i+3 на H1


def test_plateau_equal_extremes_not_confirmed(db, cfg, instrument_id):
    """Равные экстремумы (плато) строгим правилом 3+3 не подтверждаются."""
    zid = _ob(db, instrument_id)
    _visit(db, zid, _day(5), _day(7) + D1_MS - 1, extreme=104.0)
    scanner = Scanner(db, cfg)
    candles = _d1_candles({5: 104.0, 6: 104.0}, days=10, instrument_id=instrument_id)
    for c in candles:
        scanner.on_closed_candle(c)
    levels = db.list_inner_levels(parent_ob_id=zid)
    assert len(levels) == 1
    assert levels[0].status == "candidate"     # плато — не действующий уровень
    assert levels[0].confirmed_at is None
