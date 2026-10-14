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
