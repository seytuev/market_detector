from dataclasses import replace
import random

import pytest

from app.engine.ltf import context
from app.engine.ltf.lifecycle_index import LifecycleIndex
from app.engine.ltf.pivots import PivotCandidate
from app.engine.ltf.relevance import activation_history, prime_activation_history
from app.models import Direction
from tests.conftest import make_candle, H1_MS


@pytest.mark.parametrize('direction', [Direction.BULL, Direction.BEAR])
def test_indexed_counter_sweeps_match_scalar_with_gaps_equality_and_future(direction):
    rng = random.Random(121)
    candles = [make_candle(i*H1_MS, 100, 100+rng.randrange(0, 9),
                           100-rng.randrange(0, 9), 100+rng.randrange(-4, 5), 'H1')
               for i in range(180) if i % 17 != 4]
    candles = [replace(c, high=max(c.high, c.close), low=min(c.low, c.close), closed=i%23!=2)
               for i,c in enumerate(candles)]
    pivots = [PivotCandidate(instrument_id=1, kind='low' if i%2 else 'high',
                            price=96+i%9, pivot_at=i*H1_MS, candle_open_time=i*H1_MS, confirmed_at=(i+3)*H1_MS,
                            left=3, right=3, pivot_id=i+1)
              for i in range(100)]
    index = LifecycleIndex(candles, candles[-1].close_time)
    for end in (20, 60, 100, len(candles)):
        sample = candles[:end]
        for since in (0, 9*H1_MS, 15*H1_MS, 31*H1_MS):
            for cursor in (None, since+8*H1_MS, sample[-1].close_time):
                expected = context.detect_counter_sweeps(direction, pivots, sample, since,
                                                         only_after=cursor, ordered=True)
                actual = context.detect_counter_sweeps(direction, pivots, sample, since,
                                                       only_after=cursor, ordered=True, index=index)
                assert actual == expected


@pytest.mark.parametrize('direction', [Direction.BULL, Direction.BEAR])
def test_indexed_context_preserves_first_extreme_and_causal_prefix(db, monkeypatch, direction):
    candles = [make_candle(i*H1_MS, 100, 101+i%7, 99-i%6, 100, 'H1') for i in range(70)]
    monkeypatch.setattr(context, 'find_htf_fvg50_test', lambda *args, **kwargs:
                        {'zone_id':1, 'tf':'D1', 'confirmed':True, 'source_time':kwargs['event_time']})
    for end in (13, 35, 70):
        sample = candles[:end]
        for since in (0, 4*H1_MS, sample[-1].close_time+1):
            expected = context.scenario_context(db, 1, direction, [], sample, since,
                                                as_of=sample[-1].close_time, ordered=True)
            with activation_history(db):
                prime_activation_history(db, 1, candles)
                actual = context.scenario_context(db, 1, direction, [], sample, since,
                                                  as_of=sample[-1].close_time, ordered=True)
            assert actual == expected
