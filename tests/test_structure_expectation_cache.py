from copy import deepcopy
from dataclasses import replace

import pytest

from app.engine.ltf import breaks
from app.models import Direction
from app.services import structure_expectations as projection
from tests.test_ltf_breaks import (
    _series, _pivots_with_roles, SERIES_A_HL, SERIES_A_CLOSES,
    SERIES_D_HL, SERIES_D_CLOSES,
)


class Store:
    pass


@pytest.mark.parametrize('direction', list(Direction))
@pytest.mark.parametrize('kind', ['expected_structure_conditions', 'expected_reverse_condition'])
def test_projection_matches_causal_prefixes_and_revisions(kind, direction):
    db = Store()
    bars = _series(SERIES_A_HL, SERIES_A_CLOSES)
    pivots = _pivots_with_roles(bars)
    project, scalar = getattr(projection, kind), getattr(breaks, kind)
    for bar in bars:
        at = bar.close_time
        options = {'since_ms': bars[4].open_time}
        if kind == 'expected_reverse_condition':
            options['cancel_not_before_ms'] = bars[8].open_time
        assert project(pivots, bars, direction, at, db=db, **options) == scalar(
            pivots, bars, direction, at, **options)
        revised = [replace(c, close=c.close + .25) for c in bars]
        assert project(pivots, revised, direction, at, db=db, **options) == scalar(
            pivots, revised, direction, at, **options)


def test_quotes_forming_bars_and_clock_reuse_but_closed_inputs_invalidate(monkeypatch):
    bars = _series(SERIES_D_HL, SERIES_D_CLOSES)
    pivots = _pivots_with_roles(bars)
    scalar = breaks.expected_structure_conditions
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return scalar(*args, **kwargs)
    monkeypatch.setattr(breaks, 'expected_structure_conditions', counted)
    db = Store()
    at = bars[-1].close_time
    def run(ps=pivots, cs=bars, moment=at, store=db):
        return projection.expected_structure_conditions(ps, cs, Direction.BEAR, moment, db=store)
    initial = run()
    mutated = run(); mutated['bos']['status'] = 'caller mutation'
    assert run(moment=at+1000) == initial
    assert run(cs=bars+[replace(bars[-1], closed=False, close=999)]) == initial
    assert len(calls) == 1
    revised = deepcopy(bars); revised[-1].close += 1
    run(cs=revised); assert len(calls) == 2
    roles = deepcopy(pivots); roles[-1].role = 'LL'
    run(ps=roles); assert len(calls) == 3
    run(store=Store()); assert len(calls) == 4
    other = [replace(c, instrument_id=2) for c in bars]
    run(cs=other); assert len(calls) == 5
