"""PD proximity, mandatory objects and quote-independent lifecycle caching."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import Direction
from app.models_ltf import LtfEntryZone
from app.services import h1_chart
from app.services.h1_chart import select_pd_zones
from app.web.api import create_app
from tests.conftest import H1_MS, make_candle


def zone(zid, lower, upper, **changes):
    return dict(id=zid, type=('OB', 'FVG', 'BSL', 'SSL')[zid % 4],
                lower=lower, upper=upper, formed_at=zid, confirmed_at=100,
                candidate=False, lifecycle='active', validity='fresh',
                relevance={'relevant': True, 'reason': 'ok'}, **changes)


def test_five_total_on_each_side_and_all_pd_intersections():
    above = [zone(i, 110 + i, 111 + i) for i in range(1, 9)]
    below = [zone(10 + i, 89 - i, 90 - i) for i in range(1, 9)]
    inside = [zone(30, 95, 105), zone(31, 89, 91), zone(32, 109, 111),
              zone(33, 110, 110), zone(34, 90, 90)]
    chosen, info = select_pd_zones(list(reversed(above + below + inside)), {'lower': 90, 'upper': 110})
    assert {z['id'] for z in chosen} == {1, 2, 3, 4, 5, 11, 12, 13, 14, 15, 30, 31, 32, 33, 34}
    assert (info['inside'], info['above'], info['below']) == (5, 5, 5)


def test_nearest_uses_boundary_and_pins_survive_limit():
    zones = [zone(i, 110 + i, 111 + i) for i in range(1, 9)]
    # This wide zone has the closest near edge despite its distant midpoint.
    zones.append(zone(90, 110.5, 1000))
    chosen, _ = select_pd_zones(zones, {'lower': 90, 'upper': 110},
                                mandatory_ids=[7], selected_zone_id='8')
    assert {z['id'] for z in chosen} == {90, 1, 2, 3, 4, 7, 8}


@pytest.mark.parametrize('change', [
    {'candidate': True}, {'confirmed_at': None}, {'lifecycle': 'ended'},
    {'validity': 'invalid'}, {'relevance': {'relevant': False, 'reason': 'data_gap'}},
])
def test_pinning_does_not_resurrect_irrelevant_zones(change):
    row = zone(1, 95, 105)
    row.update(change)
    chosen, _ = select_pd_zones([row], {'lower': 90, 'upper': 110},
                                mandatory_ids=[1], selected_zone_id='1')
    assert chosen == []


@pytest.mark.parametrize('setup', [None, {}, {'lower': 90, 'upper': None}, {'lower': 90, 'upper': 90}])
def test_no_pd_keeps_only_mandatory(setup):
    chosen, info = select_pd_zones([zone(1, 100, 101), zone(2, 200, 201)],
                                   setup, mandatory_ids=[2])
    assert [z['id'] for z in chosen] == [2]
    assert info['pd_available'] is False


@pytest.fixture
def live(db, instrument_id, monkeypatch, tmp_path):
    start = 1_780_000_000_000 // H1_MS * H1_MS
    moment = start + 10 * H1_MS
    monkeypatch.setattr(h1_chart, 'now_ms', lambda: moment)
    monkeypatch.setattr(h1_chart, '_local_setup', lambda *args: {'lower': 90, 'upper': 110})
    db.insert_candles([make_candle(start + i * H1_MS, 100, 101, 99, 100,
                                 timeframe='H1', instrument_id=instrument_id) for i in range(10)])
    db.set_meta(f'ltf:h1:last_close:{instrument_id}', str(moment - 1))
    db.set_quote(instrument_id, 100, moment)
    stored = [db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type='OB', direction=Direction.BEAR,
        lower=150 + i * 10, upper=155 + i * 10, formed_at=start,
        confirmed_at=start + H1_MS,
    )) for i in range(8)]
    settings = Settings()
    settings.db_path = str(tmp_path / 'test.db')
    settings.auth_token = 'test'
    return settings, stored, moment


def test_live_projection_reused_for_quotes_and_invalidated_by_zone_update(db, instrument_id, live, monkeypatch):
    settings, stored, moment = live
    original = h1_chart._build_zone_projection
    calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(h1_chart, '_build_zone_projection', counted)
    def read(**kwargs):
        return h1_chart.assemble_h1_layers(db, settings, instrument_id, nearby_zones=True, **kwargs)
    assert len(read()['detected_zones']) == 5
    db.set_quote(instrument_id, 102, moment + 100)
    assert len(read()['detected_zones']) == 5
    assert len(calls) == 1
    pinned = read(selected_zone_id=str(stored[-1].id), reference_price=stored[-2].lower)
    assert {stored[-1].id, stored[-2].id} <= {z['id'] for z in pinned['detected_zones']}
    assert len(calls) == 1
    db.update_ltf_entry_zone(stored[0].id, validity='invalid')
    updated = read()
    assert stored[0].id not in {z['id'] for z in updated['detected_zones']}
    assert len(updated['detected_zones']) == 5
    assert len(calls) == 2
    # History has its own projection; the compact read must not truncate it.
    history = read(zone_history=True)
    assert len(history['detected_zones']) == 8
    assert history['zone_selection']['mode'] == 'history'
    assert len(calls) == 3
    read(as_of=moment)
    read(as_of=moment)
    assert len(calls) == 5  # Explicit historical cutoffs bypass the live cache.


def test_http_structure_and_workspace_use_pd_selection(db, instrument_id, live):
    settings, stored, _ = live
    client = TestClient(create_app(db, settings))
    headers = {'Authorization': 'Bearer test'}
    path = f'/api/ltf/instruments/{instrument_id}'
    data = client.get(path + f'/structure?nearby_zones=true&selected_zone_id={stored[-1].id}', headers=headers).json()
    assert data['zone_selection']['mode'] == 'current_pd'
    assert len(data['detected_zones']) == 6
    assert stored[-1].id in {z['id'] for z in data['detected_zones']}
    workspace = client.get(path + '/workspace', headers=headers).json()
    assert len(workspace['h1']['detected_zones']) == 5
    assert workspace['h1']['snapshot']['state_version'] == workspace['view']['state_version']
    history = client.get(path + '/structure?nearby_zones=true&zone_history=true', headers=headers).json()
    assert len(history['detected_zones']) == 8
    assert history['zone_selection']['mode'] == 'history'


def test_pd_and_price_selection_are_not_cached_with_zone_catalog(db, instrument_id, live, monkeypatch):
    settings, stored, _ = live
    first = h1_chart.assemble_h1_layers(db, settings, instrument_id, nearby_zones=True)
    assert len(first['detected_zones']) == 5
    monkeypatch.setattr(h1_chart, '_local_setup', lambda *args: {'lower': 145, 'upper': 230})
    changed = h1_chart.assemble_h1_layers(db, settings, instrument_id, nearby_zones=True)
    assert len(changed['detected_zones']) == 8
    assert changed['zone_selection']['inside'] == 8


def test_new_closed_h1_invalidates_catalog(db, instrument_id, live, monkeypatch):
    settings, stored, moment = live
    first = h1_chart.assemble_h1_layers(db, settings, instrument_id, nearby_zones=True)
    assert stored[0].id in {z['id'] for z in first['detected_zones']}
    db.insert_candles([make_candle(moment, 100, 160, 99, 158,
                                 timeframe='H1', instrument_id=instrument_id)])
    monkeypatch.setattr(h1_chart, 'now_ms', lambda: moment + H1_MS)
    changed = h1_chart.assemble_h1_layers(db, settings, instrument_id, nearby_zones=True)
    assert stored[0].id not in {z['id'] for z in changed['detected_zones']}
