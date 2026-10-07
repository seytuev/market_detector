"""§15 (Этап 8): коррекционная миграция LTF v2 (tools/migrate_ltf_correction_v2.py).

Сеянное «старое» состояние (потерянная отмена + пройденный BSL снова fresh),
dry-run ничего не пишет, apply пересчитывает канон (отмена с reverse-trio,
level_broken), delivered-метки журнала переносятся, повторный apply — «уже
выполнено» (идемпотентность).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Instrument
from tests.test_ltf_breaks import _series
from tests.test_ltf_engine import (
    SERIES_H2_CLOSES,
    SERIES_H2_HL,
    SERIES_H_HL,
    T0,
    _feed,
    _setup,
)

from tools.migrate_ltf_correction_v2 import MIGRATION_KEY, main as migration_main

TABLES = [
    "ltf_observation", "ltf_scenario", "ltf_structure_event", "ltf_movement",
    "ltf_range", "ltf_entry_zone", "ltf_scenario_entry", "ltf_liquidity_test",
    "ltf_event", "ltf_pivot",
]


def _dump(path: Path) -> dict:
    db = Database(str(path))
    out = {
        t: [dict(r) for r in db.conn.execute(f"SELECT * FROM {t} ORDER BY id")]
        for t in TABLES
    }
    db.close()
    return out


def _seed_buggy(path: Path) -> dict:
    """Канонический прогон (sc1 отменён обратным сломом на idx24), затем
    повреждение до «старого» состояния: отмена потеряна, пройденный BSL
    снова fresh/ok, delivered-метка на bos (уже отправленное уведомление)."""
    db = Database(str(path))
    iid = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    engine = LtfEngine(db, DetectorConfig())
    candles = _series(SERIES_H_HL + SERIES_H2_HL, SERIES_H2_CLOSES, iid)
    zid = _setup(db, iid)
    obs = engine.on_htf_zone_touched(iid, db.get_zone(zid), T0)
    _feed(db, engine, iid, candles, 24)
    sc1 = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc1.state == "cancelled" and sc1.reverse_break_level_price is not None
    bos = [e for e in db.list_ltf_events(observation_id=obs.id, limit=1000)
           if e.kind == "bos"][0]
    db.mark_ltf_event_delivered(bos.id)

    rev = [e for e in db.list_ltf_structure_events(sc1.id)
           if e.direction == Direction.BULL][0]
    db.conn.execute("DELETE FROM ltf_structure_event WHERE id=?", (rev.id,))
    db.conn.execute("DELETE FROM ltf_event WHERE kind='cancellation'")
    db.conn.execute(
        "UPDATE ltf_scenario SET state='monitoring_entries', "
        "cancellation_reason=NULL, cancelled_at=NULL, "
        "reverse_break_level_price=NULL, reverse_break_pivot_id=NULL, "
        "reverse_break_confirmed_at=NULL WHERE id=?", (sc1.id,))
    db.conn.execute("UPDATE ltf_observation SET state='active' WHERE id=?",
                    (obs.id,))
    z = [z for z in db.list_ltf_entry_zones(instrument_id=iid)
         if z.type == "BSL" and z.lower == 9.5][0]
    db.conn.execute(
        "UPDATE ltf_entry_zone SET validity='fresh', first_test_at=NULL "
        "WHERE id=?", (z.id,))
    db.conn.execute("DELETE FROM ltf_liquidity_test WHERE entry_zone_id=?",
                    (z.id,))
    db.conn.execute(
        "UPDATE ltf_scenario_entry SET state='fresh', reason='ok', eligible=1 "
        "WHERE entry_zone_id=?", (z.id,))
    db.conn.commit()
    db.close()
    return {"obs_id": obs.id, "scenario_id": sc1.id, "zone_id": z.id}


def _run(path: Path, *argv: str) -> None:
    import sys

    old = sys.argv
    sys.argv = ["migrate_ltf_correction_v2", "--db", str(path), *argv]
    try:
        migration_main()
    finally:
        sys.argv = old


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "dry.db"
    _seed_buggy(path)
    before = _dump(path)
    _run(path)  # без --apply
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "cancelled_without_reverse_provenance" in out
    assert _dump(path) == before
    db = Database(str(path))
    assert db.get_meta(MIGRATION_KEY) is None
    bak = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%_bak_ltfv2'"
    ).fetchall()
    db.close()
    assert bak == []


def test_apply_recalculates_and_preserves_delivered(tmp_path, capsys):
    path = tmp_path / "apply.db"
    seeded = _seed_buggy(path)
    _run(path, "--apply")
    out = capsys.readouterr().out
    assert "Итог пересчёта" in out

    db = Database(str(path))
    obs_id = seeded["obs_id"]
    scenarios = db.list_ltf_scenarios(observation_id=obs_id)
    assert len(scenarios) == 1
    sc = scenarios[0]
    # восстановлена отмена с provenance уровня отмены (Этапы 2/5)
    assert sc.state == "cancelled"
    assert sc.cancellation_reason == "reverse_bos"
    assert sc.reverse_break_level_price is not None
    assert sc.reverse_break_pivot_id is not None
    assert sc.reverse_break_confirmed_at is not None
    # пройденный BSL не стал fresh: терминальный исход + level_broken
    z = [x for x in db.list_ltf_entry_zones(instrument_id=1)
         if x.type == "BSL" and x.lower == 9.5][0]
    assert z.validity == "tested"
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == z.id]
    assert tests and tests[-1].state == "failed"
    entries = [e for e in db.list_ltf_scenario_entries(sc.id)
               if e.entry_zone_id == z.id]
    assert entries and all(e.reason == "level_broken" for e in entries)
    # журнал: отмена восстановлена как аудит (delayed, replay), pending = 0
    cancel = [e for e in db.list_ltf_events(observation_id=obs_id, limit=1000)
              if e.kind == "cancellation"]
    assert len(cancel) == 1
    assert cancel[0].delayed is True
    assert cancel[0].processing_mode == "replay"
    assert db.pending_ltf_events() == []
    # delivered-метка bos перенесена на пересчитанное событие
    bos = [e for e in db.list_ltf_events(observation_id=obs_id, limit=1000)
           if e.kind == "bos"]
    assert len(bos) == 1 and bos[0].delivered is True
    # done-ключ и табличные бэкапы
    assert db.get_meta(MIGRATION_KEY) is not None
    bak = db.conn.execute(
        "SELECT COUNT(*) FROM ltf_event_bak_ltfv2 WHERE delivered=1"
    ).fetchone()[0]
    assert bak >= 1
    db.close()


def test_apply_idempotent_second_run(tmp_path, capsys):
    path = tmp_path / "twice.db"
    _seed_buggy(path)
    _run(path, "--apply")
    capsys.readouterr()
    state = _dump(path)
    _run(path, "--apply")
    out = capsys.readouterr().out
    assert "уже выполнена" in out and "0 изменений" in out
    assert _dump(path) == state


def test_apply_requires_live_flag_for_live_db(tmp_path, monkeypatch):
    import tools.migrate_ltf_correction_v2 as m2

    path = tmp_path / "htf_zones.db"
    _seed_buggy(path)
    monkeypatch.setattr(m2, "LIVE_DB", path)
    with pytest.raises(SystemExit):
        _run(path, "--apply")
