"""Legacy FVG fill comes from visits, independently of OB entry statistics."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import Direction, Visit, Zone, ZoneStatus, ZoneType
from app.services.zone_display import fvg_fill_depths
from app.web.api import create_app


def fvg(db, iid, direction=Direction.BULL, formed_at=1000):
    zid = db.insert_zone(Zone(None, iid, ZoneType.FVG, direction, 'D1',
                             lower=.0492, upper=.05532, formed_at=formed_at,
                             confirmed_at=formed_at + 1000, status=ZoneStatus.WEAKENED))
    return db.get_zone(zid)


@pytest.mark.parametrize('direction', [Direction.BULL, Direction.BEAR])
def test_legacy_fvg_fill_matches_list_and_detail_without_rewriting_state(db, instrument_id, tmp_path, direction):
    zone = fvg(db, instrument_id, direction)
    db.open_visit(Visit(None, zone.id, zone.cycle_id, 3000, max_depth=.6))
    db.open_visit(Visit(None, zone.id, zone.cycle_id, 4000, max_depth=.1))
    # A previous cycle's deeper visit must not override the current cycle.
    db.open_visit(Visit(None, zone.id, zone.cycle_id + 1, 5000, max_depth=1))
    settings = Settings(auth_token='display-token', db_path=str(tmp_path / 'fixture.db'))
    client = TestClient(create_app(db, settings))
    headers = {'Authorization': 'Bearer display-token'}
    version = db.get_domain('seq:zones')
    for versioned in [False, True]:
        payload = client.get('/api/zones', params={
            'instrument_id': instrument_id, 'with_version': str(versioned).lower(),
        }, headers=headers).json()
        rows = payload['zones'] if versioned else payload
        assert rows[0]['fill_depth'] == pytest.approx(.6)
        assert rows[0]['max_test_depth'] == 0
    detail = client.get(f'/api/zones/{zone.id}', headers=headers).json()['zone']
    assert detail['fill_depth'] == pytest.approx(.6)
    assert db.get_domain('seq:zones') == version
    assert db.get_zone(zone.id).max_test_depth == 0


def test_fill_depths_are_batched_and_clamped(db, instrument_id):
    zones = [fvg(db, instrument_id, formed_at=1000 + i) for i in range(40)]
    db.open_visit(Visit(None, zones[0].id, 1, 3000, max_depth=1.2))
    queries = []
    db.conn.set_trace_callback(queries.append)
    depths = fvg_fill_depths(db, zones)
    db.conn.set_trace_callback(None)
    assert depths[zones[0].id] == 1
    assert depths[zones[-1].id] == 0
    assert len([q for q in queries if 'FROM visit' in q]) == 1


def test_visit_depth_refreshes_chart_domain_without_recalculating_structure(db, instrument_id):
    zone = fvg(db, instrument_id)
    version = db.get_domain('seq:zones')
    structural = db.instrument_domain('seq:zones', instrument_id)
    vid = db.open_visit(Visit(None, zone.id, 1, 3000, max_depth=.2))
    assert db.get_domain('seq:zones') > version
    version = db.get_domain('seq:zones')
    db.update_visit_depth(vid, .6)
    assert db.get_domain('seq:zones') > version
    assert fvg_fill_depths(db, [zone])[zone.id] == pytest.approx(.6)
    assert db.instrument_domain('seq:zones', instrument_id) == structural
