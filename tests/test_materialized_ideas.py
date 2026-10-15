import json
from dataclasses import asdict, replace

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.models import Instrument
from app.models_ltf import LtfEvent
from app.services import htf_ideas as ideas
from app.services.projection_cache import ProjectionCache
from tests.conftest import H1_MS
from tests.test_htf_ideas import seed, bars, T

MOMENT = T + 20 * H1_MS + 1000


def publish(db, iid, monkeypatch, moment=MOMENT):
    monkeypatch.setattr(ideas, 'now_ms', lambda: MOMENT)
    previous = getattr(db._local, 'calculation_instrument', None)
    db._local.calculation_instrument = iid
    try:
        return ideas.reconcile_ideas(db, iid, DetectorConfig(), moment)
    finally:
        db._local.calculation_instrument = previous


def test_committed_projection_is_exact_after_memory_cache_loss(db, instrument_id, monkeypatch):
    seed(db, instrument_id); bars(db, instrument_id, 10)
    publish(db, instrument_id, monkeypatch)
    exact = ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT)
    assert exact[0] and all(row['id'] is not None for row in exact[0])
    monkeypatch.setattr(ideas, '_PUBLISHED_PROJECTIONS', ProjectionCache())
    def no_join(*args, **kwargs):
        raise AssertionError('first live read must use the committed snapshot')
    monkeypatch.setattr(db, 'ltf_projection_bundle', no_join)
    assert ideas.projection_checkpoint_fresh(db, instrument_id, DetectorConfig(), MOMENT)
    result = ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT, live=True)
    assert result == exact
    result[0][0]['state'] = 'caller mutation'
    next(iter(result[2].values()))[0].evidence['caller mutation'] = True
    assert ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT+1, live=True) == exact


def test_parent_quote_depth_keeps_snapshot_but_invalidation_does_not(db, instrument_id, monkeypatch):
    parent, _, _, _ = seed(db, instrument_id); bars(db, instrument_id, 10)
    publish(db, instrument_id, monkeypatch)
    exact = ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT)
    db.update_zone(parent, max_test_depth=.5, test_extreme=110)
    assert ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT) == exact
    monkeypatch.setattr(ideas, '_PUBLISHED_PROJECTIONS', ProjectionCache())
    monkeypatch.setattr(db, 'ltf_projection_bundle', lambda *a, **k: pytest.fail('quote depth forced historical joins'))
    assert ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT, live=True) == exact
    db.update_zone(parent, market_validity='invalid')
    assert not ideas.projection_checkpoint_fresh(db, instrument_id, DetectorConfig(), MOMENT)


def test_adding_another_asset_keeps_projection_but_real_rule_changes_invalidate(db, instrument_id, monkeypatch):
    from app.services.projection_cache import structural_version
    seed(db, instrument_id); bars(db, instrument_id, 10)
    publish(db, instrument_id, monkeypatch)
    ideas.mark_ideas_checkpoint(db, instrument_id)
    expected = ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT)
    before = structural_version(db, instrument_id, DetectorConfig())
    other = db.upsert_instrument(Instrument(None, 'NEW', 'binance', 'spot', 'NEWUSDT', 'USDT', enabled=False))
    db.set_instrument_active(other, True)
    assert structural_version(db, instrument_id, DetectorConfig()) == before
    assert ideas.ideas_checkpoint_fresh(db, instrument_id)
    monkeypatch.setattr(ideas, '_PUBLISHED_PROJECTIONS', ProjectionCache())
    monkeypatch.setattr(db, 'ltf_projection_bundle', lambda *a, **k: pytest.fail('other asset invalidated history'))
    assert ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT, live=True) == expected
    changed = replace(DetectorConfig(), entry_reuse_max_depth=.5)
    assert structural_version(db, instrument_id, changed) != before
    assert not ideas.projection_checkpoint_fresh(db, instrument_id, changed, MOMENT)
    db.set_meta('settings:active_profile', json.dumps(asdict(changed)))
    assert not ideas.ideas_checkpoint_fresh(db, instrument_id)


def test_projection_survives_database_reopen(tmp_path, monkeypatch):
    path = str(tmp_path / 'projection.db')
    db = Database(path)
    iid = db.upsert_instrument(Instrument(None, 'BTC', 'binance', 'spot', 'BTCUSDT', 'USDT'))
    seed(db, iid); bars(db, iid, 10)
    publish(db, iid, monkeypatch)
    expected = ideas.project_ideas(db, iid, DetectorConfig(), MOMENT)
    db.close()
    reopened = Database(path)
    monkeypatch.setattr(ideas, '_PUBLISHED_PROJECTIONS', ProjectionCache())
    monkeypatch.setattr(reopened, 'ltf_projection_bundle', lambda *a, **k: pytest.fail('historical join after restart'))
    try:
        with reopened.read_tx():
            assert ideas.project_ideas(reopened, iid, DetectorConfig(), MOMENT, live=True) == expected
    finally:
        reopened.close()


@pytest.mark.parametrize('mode', ['nested_rollback', 'historical', 'parent_edit', 'future_fact'])
def test_uncommitted_or_changed_inputs_are_never_materialized(db, instrument_id, monkeypatch, mode):
    parent, obs, sc, _ = seed(db, instrument_id); bars(db, instrument_id, 10)
    if mode == 'nested_rollback':
        with pytest.raises(RuntimeError):
            with db.batch_writes():
                publish(db, instrument_id, monkeypatch)
                assert db.get_meta(f'htf_ideas:projection:{instrument_id}') is None
                raise RuntimeError('rollback')
    elif mode == 'historical':
        publish(db, instrument_id, monkeypatch, moment=T + 20 * H1_MS - 1)
    elif mode == 'future_fact':
        db.insert_ltf_event(LtfEvent(id=None, observation_id=obs.id, scenario_id=sc.id,
            kind='context_update', payload={'fact': 'counter_sweep'}, occurred_at=MOMENT+5000,
            detected_at=MOMENT, dedupe_key='future-materialized-fact'))
        publish(db, instrument_id, monkeypatch)
    else:
        save = db.save_htf_ideas
        def change_parent(*args, **kwargs):
            save(*args, **kwargs)
            db.update_zone(parent, market_validity='invalid')
        monkeypatch.setattr(db, 'save_htf_ideas', change_parent)
        publish(db, instrument_id, monkeypatch)
    assert db.get_meta(f'htf_ideas:projection:{instrument_id}') is None


def test_corrupt_snapshot_falls_back_and_new_facts_invalidate(db, instrument_id, monkeypatch):
    _, obs, sc, _ = seed(db, instrument_id); bars(db, instrument_id, 10)
    publish(db, instrument_id, monkeypatch)
    key = f'htf_ideas:projection:{instrument_id}'
    snapshot = json.loads(db.get_meta(key)); snapshot['payload'] = 'invalid compressed data'
    db.set_meta(key, json.dumps(snapshot))
    monkeypatch.setattr(ideas, '_PUBLISHED_PROJECTIONS', ProjectionCache())
    assert not ideas.projection_checkpoint_fresh(db, instrument_id, DetectorConfig(), MOMENT)
    assert ideas.project_ideas(db, instrument_id, DetectorConfig(), MOMENT, live=True) == ideas.project_ideas(
        db, instrument_id, DetectorConfig(), MOMENT)
    publish(db, instrument_id, monkeypatch)
    db.insert_ltf_event(LtfEvent(id=None, observation_id=obs.id, scenario_id=sc.id,
        kind='context_update', payload={'fact': 'counter_sweep'}, occurred_at=MOMENT,
        detected_at=MOMENT, dedupe_key='new-materialized-fact'))
    assert not ideas.projection_checkpoint_fresh(db, instrument_id, DetectorConfig(), MOMENT)
