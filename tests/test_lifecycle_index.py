from dataclasses import replace
import random

import pytest

from app.config import DetectorConfig
from app.engine.ltf.lifecycle_index import LifecycleIndex
from app.engine.ltf.relevance import zone_at
from app.models import Direction
from app.models_ltf import LtfEntryZone
from tests.conftest import H1_MS, make_candle


@pytest.mark.parametrize('direction', list(Direction))
@pytest.mark.parametrize('kind', ['OB', 'FVG', 'BSL', 'SSL'])
def test_index_matches_causal_lifecycle_with_gaps_and_equal_prices(direction, kind):
    rng = random.Random(503)
    cfg = DetectorConfig()
    start = 1_800_000_000_000 // H1_MS * H1_MS
    bars = []
    for i in range(180):
        low = rng.randrange(75, 120)
        high = rng.randrange(low, 131)
        bars.append(make_candle(start+i*H1_MS, low, high, low,
                                rng.randrange(low, high+1), 'H1'))
    for sample in [bars, bars[:20]+bars[21:], [], bars[:30]]:
        for end in [start+10*H1_MS-1, start+180*H1_MS-1]:
            index = LifecycleIndex(sample, end)
            for i in range(12):
                level = kind in ('BSL', 'SSL')
                z = LtfEntryZone(None, 1, kind, direction, 100, 100 if level else 110,
                                 start+i*H1_MS, start+(i+1)*H1_MS-1)
                for zone in [z, replace(z, validity='invalid', evidence={'invalidated_at': end}),
                             replace(z, confirmed_at=None)]:
                    expected = zone_at(zone, sample, end, cfg)
                    assert index.project(zone, cfg) == expected


def test_shared_entry_history_matches_scalar_discovery():
    from app.engine.ltf.entries import EntryHistory, MovementDraft, detect_entry_zones
    from tests.test_ltf_breaks import _series, SERIES_B_HL
    bars = _series(SERIES_B_HL, {}, 1)
    history = EntryHistory(bars)
    cfg = DetectorConfig()
    for direction in Direction:
        movement = MovementDraft(0, direction, 0, 0, bars[0].open_time,
                                 bars[5].open_time, bars[0].open_time, 'break', [])
        for cap in [None, bars[8].close_time, bars[-1].close_time+1]:
            for after in [None, bars[4].close_time]:
                reference = detect_entry_zones(bars, movement, [], direction, cfg,
                                               context_until=cap, confirmed_after=after)
                indexed = detect_entry_zones(bars, movement, [], direction, cfg,
                                             context_until=cap, confirmed_after=after, history=history)
                assert indexed == reference


@pytest.mark.parametrize('kind', ['OB', 'FVG', 'BSL', 'SSL'])
@pytest.mark.parametrize('direction', list(Direction))
def test_activation_history_matches_scalar_without_per_zone_reads(kind, direction):
    from types import SimpleNamespace
    from threading import local
    from app.engine.ltf.relevance import activation_cut, activation_history, prime_activation_history
    cfg = DetectorConfig()
    start = 1_800_000_000_000 // H1_MS * H1_MS
    bars = [make_candle(start+i*H1_MS, 105, 108+i, 102-i, 105, 'H1') for i in range(30)]
    bars.pop(12)
    reads = []
    def load(*args, **kwargs):
        reads.append(args)
        return bars
    db = SimpleNamespace(_local=local(), get_candles=load)
    zone = LtfEntryZone(1, 1, kind, direction, 100, 100 if kind in ('BSL','SSL') else 110,
                        start, start+H1_MS-1)
    cuts = [start+3*H1_MS-1, start+11*H1_MS-1, start+20*H1_MS-1]
    expected = [activation_cut(db, zone, at, cfg) for at in cuts]
    reads.clear()
    with activation_history(db):
        prime_activation_history(db, 1, bars)
        for _ in range(100):
            assert [activation_cut(db, zone, at, cfg) for at in cuts] == expected
        assert reads == []
    assert db._local.activation_histories is None
    # A later independent snapshot must observe repaired/revised inputs.
    bars[:] = [replace(c, high=106, low=104) for c in bars]
    changed = activation_cut(db, zone, cuts[0], cfg)
    reads.clear()
    with activation_history(db):
        assert activation_cut(db, zone, cuts[0], cfg) == changed
        assert len(reads) == 1


def test_current_pivots_select_latest_row_before_loading_histories(db, instrument_id):
    from app.models_ltf import LtfPivot
    base = LtfPivot(id=None, instrument_id=instrument_id, kind='high', price=100,
                    pivot_at=1000, candle_open_time=1000, confirmed_at=2000)
    # Preserve all historical IDs, including stores with unmarked duplicates.
    ids = [db.insert_ltf_pivot(replace(base, price=100+i)) for i in range(50)]
    current = db.list_ltf_pivots(instrument_id)
    assert len(current) == 1
    assert current[0].id == ids[-1]
    assert current[0].price == 149
    assert len(db.list_ltf_pivots(instrument_id, include_superseded=True)) == 50
