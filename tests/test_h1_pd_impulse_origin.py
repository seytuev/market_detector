"""PD includes the unconfirmed base of the closed-candle BOS impulse."""
from dataclasses import replace

import pytest

from app.engine.ltf.market_structure import replay_structure
from app.services.h1_setup import H1LegCursor, local_leg_at, revision_at
from tests.conftest import make_h1_candles
from tests.test_nested_market_structure import T0, mirror, pivot


def impulse(mirrored=False):
    bars = make_h1_candles([
        (83500, 84000, 83000, 83500),
        (83500, 83600, 81500, 82000),
        (82000, 83933, 82000, 83500),
        (83500, 83500, 80500.01, 81000),
        (81000, 81983, 81000, 81500),
        (81500, 81600, 79700, 80200),
        (80200, 81800, 80100, 81700),
        (81700, 82400, 81600, 82300),
        (82300, 82500, 82000, 82400),
        (82400, 82631, 82300, 82457.95),
    ], T0, 1)
    pivots = [pivot(0, 84000, "high"), pivot(1, 81500, "low"),
              pivot(2, 83933, "high"), pivot(3, 80500.01, "low"),
              pivot(4, 81983, "high"), pivot(5, 79700, "low", 8)]
    return mirror(pivots, bars) if mirrored else (pivots, bars)


@pytest.mark.parametrize("mirrored", [False, True])
def test_main_pd_uses_observed_base_before_pivot_confirmation(mirrored):
    pivots, bars = impulse(mirrored)
    machine, events = replay_structure(pivots, bars[:8], bars[7].close_time)
    assert events[-1].kind == "BOS"
    assert events[-1].break_level == (18017 if mirrored else 81983)
    main = machine.main_structure()
    assert main["origin"] == (20300 if mirrored else 79700)
    assert main["origin_at"] == bars[5].open_time
    assert main["range_status"] == "provisional"
    # Later confirmation and a new endpoint retain the same impulse base.
    later, _ = replay_structure(pivots, bars, bars[-1].close_time)
    main = later.main_structure()
    assert main["origin"] == (20300 if mirrored else 79700)
    assert main["extreme"] == (17369 if mirrored else 82631)
    assert main["eq"] == (18834.5 if mirrored else 81165.5)
    # Open candles, and closed candles beyond as_of, cannot select the base.
    future = replace(bars[-1], open_time=bars[-1].open_time + 3600000,
                     close_time=bars[-1].close_time + 3600000,
                     low=1, high=99999, closed=False)
    unchanged, _ = replay_structure(pivots, bars + [future], future.close_time)
    assert unchanged.main_structure() == main
    unchanged, _ = replay_structure(pivots, bars + [replace(future, closed=True)], bars[-1].close_time)
    assert unchanged.main_structure() == main


@pytest.mark.parametrize("mirrored", [False, True])
def test_local_pd_and_incremental_replay_share_main_impulse_base(db, instrument_id, mirrored):
    pivots, bars = impulse(mirrored)
    bars = [replace(c, instrument_id=instrument_id) for c in bars]
    cursor = H1LegCursor(instrument_id)
    for end in (8, len(bars)):
        cursor.advance(db, pivots, bars[:end], bars[end - 1].close_time)
        leg = local_leg_at(db, instrument_id, bars[end - 1].close_time)
        assert leg["origin_price"] == (20300 if mirrored else 79700)
        assert leg["origin_at"] == bars[5].open_time
    rev = revision_at(db, leg["id"], bars[-1].close_time)
    assert rev["eq"] == (18834.5 if mirrored else 81165.5)


def test_rule_upgrade_repairs_saved_pd_even_after_an_earlier_upgrade(db, cfg, instrument_id, monkeypatch):
    import json
    from types import SimpleNamespace
    from app.engine.ltf.engine import LtfEngine
    from app.engine.ltf.market_structure import RULE_VERSION

    pivots, bars = impulse()
    db.insert_candles(bars)
    H1LegCursor(instrument_id).advance(db, pivots, bars, bars[-1].close_time)
    leg = local_leg_at(db, instrument_id, bars[-1].close_time)
    db.conn.execute("UPDATE h1_local_leg SET origin_price=80500.01 WHERE id=?", (leg["id"],))
    db.conn.execute("UPDATE h1_local_leg_revision SET lower=80500.01,eq=(80500.01+upper)/2 WHERE leg_id=?", (leg["id"],))
    db.set_meta(f"structure:rules:{instrument_id}", "structure-2026-10-14")
    db.set_meta(f"structure:legacy_scenarios:{instrument_id}", "[]")
    # Pivot detection did not change; the existing calculation version can
    # remain current when the structure/PD rule changes.
    monkeypatch.setattr("app.engine.ltf.structure.sync_structure",
                        lambda *args, **kwargs: SimpleNamespace(avail=pivots))
    engine = LtfEngine(db, cfg)
    assert not engine.process_h1_close(instrument_id, bars[-1].close_time).events
    repaired = local_leg_at(db, instrument_id, bars[-1].close_time)
    assert repaired["id"] == leg["id"]
    assert repaired["origin_price"] == 79700
    assert revision_at(db, repaired["id"], bars[-1].close_time)["eq"] == 81165.5
    assert json.loads(db.get_meta(f"h1:main_structure:{instrument_id}"))["origin"] == 79700
    assert db.get_meta(f"structure:rules:{instrument_id}") == RULE_VERSION
    before = [tuple(r) for r in db.conn.execute("SELECT * FROM h1_local_leg_revision ORDER BY id")]
    assert not engine.process_h1_close(instrument_id, bars[-1].close_time).events
    assert [tuple(r) for r in db.conn.execute("SELECT * FROM h1_local_leg_revision ORDER BY id")] == before
    assert not db.pending_ltf_events()


def test_continuation_does_not_replace_observed_base_with_older_confirmed_correction():
    from app.services.h1_setup import replay_events
    from tests.test_h1_local_pd import _bars, _bos, _pivot
    from app.models import Direction

    bars = _bars()
    first = _bos(Direction.BULL, 3, 81600, "first")
    second = _bos(Direction.BULL, 9, 83400, "continuation")
    correction = _pivot("low", 82800, 6, 7, "HL")
    second.evidence.update(origin_provenance="structural_candidate",
                           origin_at=bars[9].open_time,
                           origin_known_at=bars[9].close_time, anchor_price=82500)
    state = replay_events([first, second], [_pivot("low", 80400, 0, 2), correction],
                          bars, bars[-1].close_time)
    assert state["current"]["origin_price"] == 82500
    assert state["current"]["eq"] == (82500 + 83600) / 2
