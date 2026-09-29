"""ТЗ «Единый движок» §10/§11: миграция (приёмка N) и воспроизводимый отчёт
поиска по датированным примерам (приёмка K).
"""
from __future__ import annotations

import json

from app.config import DetectorConfig
from app.db import Database
from app.engine.scanner import Scanner
from app.models import (
    Candle,
    Direction,
    Instrument,
    TIMEFRAME_MINUTES,
    Zone,
    ZoneStatus,
    ZoneType,
)

from .conftest import make_candle

T0 = 1780272000000
D1_MS = TIMEFRAME_MINUTES["D1"] * 60_000


def _bull_ob(db, instrument_id, end_reason=None, status=ZoneStatus.WORKED,
             lower=100.0, upper=110.0, formed_at=T0):
    z = Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=lower, upper=upper,
        formed_at=formed_at, confirmed_at=formed_at + D1_MS, status=status,
        display_until=formed_at + 2 * D1_MS if end_reason else None,
        end_reason=end_reason,
    )
    return db.get_zone(db.insert_zone(z))


def _history_candles(instrument_id, closes):
    """Свечи D1 от formed_at (T0) с заданными закрытиями (high/low ±2)."""
    return [
        make_candle(T0 + i * D1_MS, c + 1, c + 2, c - 2, c, "D1",
                    instrument_id=instrument_id)
        for i, c in enumerate(closes)
    ]


def test_n_migration_restores_unbroken_and_keeps_broken(db, cfg, instrument_id):
    """Приёмка N: восстанавливаются непробитые OB, пробитые — нет; повторный
    запуск не дублирует события и уведомления."""
    from tools.restore_unified_zones import collect_targets, restore_zone

    unbroken = _bull_ob(db, instrument_id, end_reason="worked_90 (§6)")
    # пробитая зона — на другом диапазоне (свечи инструмента общие)
    broken = _bull_ob(db, instrument_id, end_reason="worked_90 (§6)",
                      lower=120.0, upper=130.0, formed_at=T0 + 1)
    # закрытия выше [100,110] (unbroken не пробит), но ниже 120 (broken пробит)
    db.insert_candles(_history_candles(instrument_id, [112.0, 111.0, 113.0]))

    targets = collect_targets(db)
    assert {z.id for z in targets["worked_90"]} == {unbroken.id, broken.id}

    now = T0 + 10 * D1_MS
    rec_ok = restore_zone(db, cfg, db.get_zone(unbroken.id), now, dry=False)
    rec_bad = restore_zone(db, cfg, db.get_zone(broken.id), now, dry=False)
    assert rec_ok["result"] == "restored"
    assert rec_bad["result"].startswith("broken")

    z_ok = db.get_zone(unbroken.id)
    assert z_ok.status == ZoneStatus.ACTIVE
    assert z_ok.market_validity == "active"
    assert z_ok.display_until is None and z_ok.end_reason is None

    z_bad = db.get_zone(broken.id)
    assert z_bad.status == ZoneStatus.ARCHIVED
    assert z_bad.market_validity == "invalid"
    assert z_bad.end_reason == "close_beyond (ТЗ §3)"
    # событие пробоя с контекстом свечи
    evs = db.get_events(broken.id)
    assert any(e.evidence.get("migration") == "restore_unified_zones" for e in evs)

    # пересчёт восстановленной зоны общим движком
    scanner = Scanner(db, cfg)
    scanner.replay_instrument(instrument_id)
    n_events = len(db.get_events(unbroken.id))
    n_deliveries = db.conn.execute("SELECT COUNT(*) c FROM delivery").fetchone()["c"]

    # повторный прогон — без дублей (идемпотентность)
    scanner2 = Scanner(db, cfg)
    scanner2.replay_instrument(instrument_id)
    assert len(db.get_events(unbroken.id)) == n_events
    assert db.conn.execute("SELECT COUNT(*) c FROM delivery").fetchone()["c"] == n_deliveries


def test_n_migration_marks_needs_replay_without_candles(db, cfg, instrument_id):
    """Нехватка свечей → needs_replay, не принудительно active (ТЗ §10 п.6)."""
    from tools.restore_unified_zones import restore_zone

    z = _bull_ob(db, instrument_id)
    rec = restore_zone(db, cfg, z, T0 + 10 * D1_MS, dry=False)
    assert rec["result"].startswith("needs_replay")
    after = db.get_zone(z.id)
    assert after.needs_replay is True
    assert after.status == ZoneStatus.WORKED  # не принудительно active


def test_k_diag_examples_reproducible(tmp_path):
    """Приёмка K: отчёт поиска по примерам воспроизводим на одних данных."""
    from tools.diag_unified_examples import main as diag_main

    db_path = tmp_path / "src.db"
    db = Database(str(db_path))
    iid = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    # простая история D1/W1, чтобы движок имел данные
    candles: list[Candle] = []
    base = 1758000000000  # 2025-09-16
    for i in range(30):
        c = 100.0 + (i % 5)
        candles.append(make_candle(base + i * D1_MS, c, c + 2, c - 2, c + 0.5,
                                   "D1", instrument_id=iid))
    for i in range(10):
        c = 100.0 + i
        candles.append(make_candle(base + i * 7 * D1_MS, c, c + 3, c - 3, c + 1,
                                   "W1", instrument_id=iid))
    db.insert_candles(candles)
    db.close()

    out1 = tmp_path / "r1.json"
    out2 = tmp_path / "r2.json"
    p1 = diag_main(db_path=db_path, out_json=out1, out_md=tmp_path / "r1.md")
    p2 = diag_main(db_path=db_path, out_json=out2, out_md=tmp_path / "r2.md")
    # воспроизводимость: одинаковые данные → одинаковые вердикты обнаружения
    assert [c["detected"] for c in p1["cases"]] == [c["detected"] for c in p2["cases"]]
    assert {c["zone_id"] for c in p1["cases"]} == {
        5727348, 5727349, 5727350, 5727351, 5727352, 5726949}
    assert json.loads(out1.read_text(encoding="utf-8"))["rule_version"] == "0.2"
