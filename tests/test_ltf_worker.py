"""Интеграция LTF в Worker (LTF-спека §4, §13, §14): запуск наблюдений от
HTF-событий, H1-цикл, восстановление после перезапуска, конфиг (п.16–17, 20–21)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import DetectorConfig, Settings, load_detector_config
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import (
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.web.api import _load_detector_from_file, _save_detector_to_file
from app.worker import SEED, Worker
from tests.conftest import H1_MS, make_candle, make_h1_candles
from tests.test_ltf_breaks import SERIES_B_HL

D1_MS = 1440 * 60_000
W1_MS = 10080 * 60_000


class FakeAdapter:
    """In-memory источник (как в test_worker): каталог SEED, свечи из списка."""

    venue = "binance"

    def __init__(self):
        self.candles = []
        self.price = (110.0, now_ms())

    async def catalog(self):
        return [
            Instrument(None, s.replace("USDT", ""), "binance", "spot", s, "USDT")
            for _, s in SEED
        ]

    async def klines(self, symbol, timeframe, start_ms, end_ms, include_forming=False):
        return [
            replace(c) for c in self.candles
            if c.timeframe == timeframe and start_ms <= c.open_time <= end_ms
        ]

    async def last_price(self, symbol):
        return self.price


def _make_worker(db, adapter, cfg: DetectorConfig | None = None, with_ltf=True):
    settings = Settings()
    settings.detector = cfg or DetectorConfig()
    dispatcher = EventDispatcher(db, settings.detector, LogSender())
    engine = LtfEngine(db, settings.detector) if with_ltf else None
    worker = Worker(db, settings, settings.detector, {"binance": adapter},
                    dispatcher, ltf_engine=engine)
    return worker, engine


def _recent_h1(hl, closes=None, instrument_id=1):
    """H1-серия, заканчивающаяся «сейчас» (в окне ltf_history_days)."""
    start = now_ms() - (len(hl) + 1) * H1_MS
    bars = []
    for i, (h, l) in enumerate(hl):
        c = closes.get(i, (h + l) / 2) if closes else (h + l) / 2
        bars.append(((h + l) / 2, h, l, c))
    return make_h1_candles(bars, start, instrument_id)


def _parent_zone(db, instrument_id, direction=Direction.BEAR):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=direction, timeframe="D1", lower=90.0, upper=100.0,
        formed_at=now_ms() - 10 * D1_MS, confirmed_at=now_ms() - 9 * D1_MS,
        status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


async def test_htf_touch_starts_observation_idempotent():
    """§4/п.20: TOUCH по валидному OB D1 открывает наблюдение; повторный
    опрос не дублирует его; H1-история до касания подгружена."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    now = now_ms()
    adapter.candles = [
        make_candle(now - D1_MS, 109, 111, 108, 110, "D1"),
        make_candle(now - W1_MS, 109, 111, 108, 110, "W1"),
        *_recent_h1([(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]),
    ]
    adapter.price = (99.0, now)  # цена внутри зоны [90;100] → TOUCH
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    zone = _parent_zone(db, ins.id, Direction.BULL)

    await worker.poll_once()
    obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
    assert obs is not None
    assert obs.state == "waiting_structure"
    assert obs.direction == Direction.BULL
    # H1-история подгружена при активации (§4)
    assert db.get_candles(ins.id, "H1")
    assert db.get_meta(f"ltf:h1:seeded:{obs.id}")

    await worker.poll_once()  # повторное HTF-событие не дублирует наблюдение
    assert len(db.list_ltf_observations(instrument_id=ins.id)) == 1


async def test_ltf_loop_processes_h1_closes():
    """§13: H1-цикл догружает свечи и гоняет process_h1_close — pivots
    материализуются, meta-курсор двигается."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _recent_h1(
        [(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]
    )
    worker, engine = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    zone = _parent_zone(db, ins.id)
    engine.on_htf_zone_touched(ins.id, zone, adapter.candles[0].open_time)

    await worker.ltf_poll_once()
    pivots = db.list_ltf_pivots(ins.id)
    assert len(pivots) == 1 and pivots[0].kind == "high" and pivots[0].price == 13
    assert db.get_meta(f"ltf:h1:last_close:{ins.id}")

    await worker.ltf_poll_once()  # без новых свечей — без изменений
    assert len(db.list_ltf_pivots(ins.id)) == 1


async def test_restart_restore_no_duplicates():
    """§13/п.20: новый Worker поверх той же БД — replay не дублирует
    pivots/события/зоны/сценарии."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _recent_h1(SERIES_B_HL, {18: 10.9})
    worker, engine = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    zone = _parent_zone(db, ins.id)
    obs = engine.on_htf_zone_touched(ins.id, zone, adapter.candles[0].open_time)

    # точка подключения доставки этапа D: ltf_dispatcher получает новые события
    delivered = []
    async def _collect(events):
        delivered.extend(events)
    worker.ltf_dispatcher = _collect

    await worker.ltf_poll_once()
    assert [e.kind for e in delivered] == ["sms"]  # сценарий открыт SMS (§6.3)
    before = {
        "obs": len(db.list_ltf_observations(instrument_id=ins.id)),
        "scenarios": len(db.list_ltf_scenarios(observation_id=obs.id)),
        "pivots": len(db.list_ltf_pivots(ins.id)),
        "zones": len(db.list_ltf_entry_zones(instrument_id=ins.id)),
        "events": len(db.list_ltf_events(observation_id=obs.id, limit=1000)),
    }
    assert before["scenarios"] == 1

    # «перезапуск»: новый Worker и новый движок поверх той же БД
    adapter2 = FakeAdapter()
    adapter2.candles = list(adapter.candles)
    worker2, _ = _make_worker(db, adapter2)
    await worker2._ltf_restore_all()
    after = {
        "obs": len(db.list_ltf_observations(instrument_id=ins.id)),
        "scenarios": len(db.list_ltf_scenarios(observation_id=obs.id)),
        "pivots": len(db.list_ltf_pivots(ins.id)),
        "zones": len(db.list_ltf_entry_zones(instrument_id=ins.id)),
        "events": len(db.list_ltf_events(observation_id=obs.id, limit=1000)),
    }
    assert after == before


async def test_ltf_opens_when_price_already_inside_parent():
    """Цена внутри активного OB D1 — наблюдение открывается без галочки
    и без нового HTF-события в текущем poll."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _recent_h1(
        [(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]
    )
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    now = now_ms()
    db.insert_candles([
        make_candle(now - D1_MS, 95, 96, 94, 95, "D1", ins.id),
    ])
    zone = _parent_zone(db, ins.id, Direction.BEAR)  # 90–100
    await worker.ltf_poll_once()
    obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
    assert obs is not None and obs.state in ("waiting_structure", "active")
    d1 = db.last_candle(ins.id, "D1")
    assert obs.activated_at == d1.open_time


async def test_ltf_analyze_flag_opens_without_touch():
    """Галочка «Анализировать»: наблюдения на подтверждённые OB D1/W1
    открываются без касания; идемпотентно; закрытые не воскрешаются."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _recent_h1(
        [(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]
    )
    adapter.price = (150.0, now_ms())  # цена вне зон — касаний нет
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    eth = next(i for i in db.get_instruments() if i.symbol == "ETHUSDT")
    zone = _parent_zone(db, ins.id, Direction.BULL)
    _parent_zone(db, eth.id, Direction.BULL)

    await worker.ltf_poll_once()  # без галочки наблюдение не открывается
    assert db.list_ltf_observations(instrument_id=ins.id) == []

    db.set_instrument_ltf_analyze(ins.id, True)
    await worker.ltf_poll_once()
    obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
    assert obs is not None and obs.state == "waiting_structure"
    assert db.get_meta(f"ltf:h1:seeded:{obs.id}")  # H1-контекст подгружен
    # галочка только на BTC — ETH без наблюдений
    assert db.list_ltf_observations(instrument_id=eth.id) == []

    await worker.ltf_poll_once()  # идемпотентно: дублей нет
    assert len(db.list_ltf_observations(instrument_id=ins.id)) == 1

    # закрытое вручную наблюдение не воскресает при включённой галочке
    db.update_ltf_observation(obs.id, state="closed_by_user")
    await worker.ltf_poll_once()
    again = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
    assert again.id == obs.id and again.state == "closed_by_user"


async def test_ltf_disabled_noop():
    """§14: ltf_enabled=False — ни запуск наблюдений, ни H1-цикл."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    now = now_ms()
    adapter.candles = [
        make_candle(now - D1_MS, 109, 111, 108, 110, "D1"),
        make_candle(now - W1_MS, 109, 111, 108, 110, "W1"),
        *_recent_h1([(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]),
    ]
    adapter.price = (99.0, now)
    cfg = DetectorConfig(ltf_enabled=False)
    worker, engine = _make_worker(db, adapter, cfg=cfg)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    _parent_zone(db, ins.id, Direction.BULL)

    await worker.poll_once()
    await worker.ltf_poll_once()
    assert db.list_ltf_observations(instrument_id=ins.id) == []
    assert db.list_ltf_pivots(ins.id) == []


def test_ltf_config_from_env(monkeypatch):
    """§14: новые поля DetectorConfig читаются из ENV (HTF_DET_*)."""
    monkeypatch.setenv("HTF_DET_LTF_ENABLED", "false")
    monkeypatch.setenv("HTF_DET_LTF_POLL_SECONDS", "123")
    monkeypatch.setenv("HTF_DET_LTF_STRUCTURE_LEFT", "4")
    cfg = load_detector_config()
    assert cfg.ltf_enabled is False
    assert cfg.ltf_poll_seconds == 123
    assert cfg.ltf_structure_left == 4
    assert cfg.ltf_range_right == 3  # дефолт спеки не переопределён


def test_ltf_config_settings_json_roundtrip(tmp_path):
    """§14: поля LTF персистятся через settings.json общим механизмом."""
    settings = Settings(db_path=str(tmp_path / "htf_zones.db"))
    settings.detector.ltf_poll_seconds = 77
    settings.detector.ltf_entry_types = "FVG,OB"
    _save_detector_to_file(settings)

    fresh = Settings(db_path=str(tmp_path / "htf_zones.db"))
    _load_detector_from_file(fresh)
    assert fresh.detector.ltf_poll_seconds == 77
    assert fresh.detector.ltf_entry_types == "FVG,OB"


# --- ТЗ «LTF Current Setup» §16.1 (предлагаемый режим): htf_context_types ---


def _fvg_zone(db, instrument_id, direction=Direction.BEAR,
              status=ZoneStatus.ACTIVE, timeframe="D1"):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=direction, timeframe=timeframe, lower=90.0, upper=100.0,
        formed_at=now_ms() - 10 * D1_MS, confirmed_at=now_ms() - 9 * D1_MS,
        status=status,
    ))
    return db.get_zone(zid)


async def test_fvg_context_opens_observation_when_enabled():
    """§16.1: при htf_context_types="OB,FVG" касание FVG D1 открывает
    наблюдение (путь «уже достигнутых родителей»); по умолчанию (только OB)
    FVG наблюдение не открывает."""
    h1 = _recent_h1([(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)])

    # включён FVG: цена внутри FVG D1 → наблюдение открывается
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = h1
    adapter.price = (95.0, now_ms())  # внутри FVG [90;100]
    cfg = DetectorConfig(htf_context_types="OB,FVG")
    worker, _ = _make_worker(db, adapter, cfg=cfg)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    now = now_ms()
    db.insert_candles([make_candle(now - D1_MS, 95, 96, 94, 95, "D1", ins.id)])
    zone = _fvg_zone(db, ins.id)
    await worker.ltf_poll_once()
    obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
    assert obs is not None and obs.state in ("waiting_structure", "active")

    # умолчание (только OB): тот же FVG наблюдение не открывает
    db2 = Database(":memory:")
    adapter2 = FakeAdapter()
    adapter2.candles = h1
    adapter2.price = (95.0, now_ms())
    worker2, _ = _make_worker(db2, adapter2)  # htf_context_types="OB"
    await worker2.seed_instruments()
    ins2 = next(i for i in db2.get_instruments() if i.symbol == "BTCUSDT")
    db2.insert_candles([make_candle(now - D1_MS, 95, 96, 94, 95, "D1", ins2.id)])
    _fvg_zone(db2, ins2.id)
    await worker2.ltf_poll_once()
    assert db2.list_ltf_observations(instrument_id=ins2.id) == []


async def test_fvg_context_on_poll_touch_event():
    """§16.1: HTF-событие TOUCH по FVG D1 в текущем poll открывает наблюдение
    при включённом FVG; по умолчанию — нет."""
    now = now_ms()
    for cfg, expected in (
        (DetectorConfig(htf_context_types="OB,FVG"), True),
        (DetectorConfig(), False),  # умолчание: только OB
    ):
        db = Database(":memory:")
        adapter = FakeAdapter()
        adapter.candles = _recent_h1([(10, 9), (11, 9), (12, 9)])
        worker, _ = _make_worker(db, adapter, cfg=cfg)
        await worker.seed_instruments()
        ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
        zone = _fvg_zone(db, ins.id)
        event = Event(
            id=None, zone_id=zone.id, cycle_id=zone.cycle_id,
            kind=EventKind.TOUCH, occurred_at=now, detected_at=now, price=95.0,
        )
        await worker._ltf_on_poll(ins, [event])
        obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
        assert (obs is not None) is expected, cfg.htf_context_types


async def test_fvg_weakened_stays_valid_parent():
    """§16.1: касание 50% НЕ прекращает FVG — зона в статусе WEAKENED
    (качественная пометка общего движка) остаётся валидным родителем:
    событие FVG_WEAKENED открывает наблюдение, уже достигнутая WEAKENED-зона
    тоже."""
    now = now_ms()
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _recent_h1([(10, 9), (11, 9), (12, 9)])
    cfg = DetectorConfig(htf_context_types="OB,FVG")
    worker, _ = _make_worker(db, adapter, cfg=cfg)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    zone = _fvg_zone(db, ins.id, status=ZoneStatus.WEAKENED)
    event = Event(
        id=None, zone_id=zone.id, cycle_id=zone.cycle_id,
        kind=EventKind.FVG_WEAKENED, occurred_at=now, detected_at=now,
        price=95.0, depth=0.5,
    )
    await worker._ltf_on_poll(ins, [event])
    obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
    assert obs is not None


def test_htf_context_types_parser():
    """§16.1: парсер настройки — только OB/FVG; PRB/Breaker/BSL/SSL
    отбрасываются (триггерами не являются)."""
    assert DetectorConfig().htf_context_type_set() == {"OB"}
    assert DetectorConfig(
        htf_context_types="OB,FVG"
    ).htf_context_type_set() == {"OB", "FVG"}
    assert DetectorConfig(
        htf_context_types=" fvg , ob "
    ).htf_context_type_set() == {"OB", "FVG"}
    assert DetectorConfig(
        htf_context_types="OB,FVG,PRB,BREAKER,BSL,SSL"
    ).htf_context_type_set() == {"OB", "FVG"}
    assert DetectorConfig(htf_context_types="").htf_context_type_set() == set()
