"""Hyperliquid @207 и HYPE — один спот. В списке остаётся публичное имя HYPE."""
from __future__ import annotations

from app.db import Database
from app.models import Candle, Instrument


def _ins(db: Database, symbol: str, enabled: bool = True) -> int:
    return db.upsert_instrument(Instrument(
        None, "HYPE", "hyperliquid", "spot", symbol, "USDT0", enabled=enabled,
    ))


def _candles(db: Database, instrument_id: int, count: int) -> None:
    base = 1_700_000_000_000
    db.insert_candles([
        Candle(
            instrument_id, "H1", base + i * 3_600_000,
            base + (i + 1) * 3_600_000 - 1,
            1, 2, 0.5, 1.5, True, "test",
        )
        for i in range(count)
    ])


def _public(db: Database) -> list[Instrument]:
    return [i for i in db.get_instruments() if i.venue == "hyperliquid"]


def test_richer_alias_keeps_id_and_empty_hype_is_deleted():
    db = Database(":memory:")
    alias = _ins(db, "@207")
    hype = _ins(db, "HYPE")
    _candles(db, alias, 3)
    db.collapse_hyperliquid_hype()
    winner = db.get_instrument(alias)
    assert winner is not None and winner.symbol == "HYPE" and winner.enabled
    assert db.get_instrument(hype) is None
    public = _public(db)
    assert len(public) == 1 and public[0].id == alias


def test_both_with_candles_richer_hype_stays_alias_is_retired():
    db = Database(":memory:")
    alias = _ins(db, "@207", enabled=True)
    hype = _ins(db, "HYPE", enabled=True)
    _candles(db, alias, 2)
    _candles(db, hype, 5)
    db.collapse_hyperliquid_hype()
    kept = db.get_instrument(hype)
    assert kept is not None and kept.symbol == "HYPE" and kept.enabled
    retired = db.get_instrument(alias)
    assert retired is not None
    assert retired.symbol == f"@207#retired-{alias}"
    assert retired.enabled is False
    assert [i.symbol for i in _public(db)] == ["HYPE"]
    hidden = db.get_instruments(include_retired=True)
    assert any(i.symbol == f"@207#retired-{alias}" for i in hidden)


def test_sole_disabled_hype_stays_disabled():
    db = Database(":memory:")
    iid = _ins(db, "HYPE", enabled=False)
    db.collapse_hyperliquid_hype()
    row = db.get_instrument(iid)
    assert row is not None and row.symbol == "HYPE" and row.enabled is False


def test_sole_alias_is_renamed_to_hype():
    db = Database(":memory:")
    iid = _ins(db, "@207", enabled=True)
    _candles(db, iid, 1)
    db.collapse_hyperliquid_hype()
    row = db.get_instrument(iid)
    assert row is not None and row.symbol == "HYPE" and row.enabled
    assert [i.id for i in _public(db)] == [iid]
