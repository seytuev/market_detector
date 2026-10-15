from dataclasses import replace

from app.models import Instrument
from tests.test_htf_ideas import seed, T
from tests.conftest import H1_MS


def test_context_batch_preserves_latest_active_and_independent_parent_models(db, instrument_id, monkeypatch):
    parent, obs, sc, _ = seed(db, instrument_id)
    cancelled = db.insert_ltf_scenario(replace(sc, id=None, state='cancelled'))
    other = db.upsert_instrument(Instrument(None, 'ETH', 'binance', 'spot', 'ETHUSDT', 'USDT'))
    _, other_obs, other_sc, _ = seed(db, other)
    execute = db.conn.execute
    queries = []
    def measured(sql, *args, **kwargs):
        queries.append(sql)
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(db.conn, 'execute', measured)
    with db.read_tx():
        db.prime_ltf_context_reads([obs, other_obs])
        queries.clear()
        assert db.get_active_ltf_scenario(obs.id).id == sc.id
        assert db.get_active_ltf_scenario(other_obs.id).id == other_sc.id
        zone = db.get_zone(parent); zone.lower = -100
        assert db.get_zone(parent).lower != -100
        assert not queries
    db.update_ltf_scenario(sc.id, state='closed')
    assert db.get_active_ltf_scenario(obs.id) is None
    db.update_ltf_scenario(cancelled.id, state='range_pending')
    db.update_zone(parent, lower=90)
    with db.read_tx():
        db.prime_ltf_context_reads([obs])
        assert db.get_zone(parent).lower == 90
        assert db.get_active_ltf_scenario(obs.id).id == cancelled.id


def test_live_bundle_reuses_ltf_rows_across_parent_quotes_but_not_ltf_changes(db, instrument_id, monkeypatch):
    parent, _, sc, _ = seed(db, instrument_id)
    moment = T + 20 * H1_MS
    with db.read_tx():
        initial = db.ltf_projection_bundle(instrument_id, moment, live=True)
        assert initial == db.ltf_projection_bundle(instrument_id, moment)
    db.set_quote(instrument_id, 105, moment+100)
    db.update_zone(parent, max_test_depth=.3)
    execute = db.conn.execute
    joins = []
    def measured(sql, *args, **kwargs):
        if 'WITH scope AS' in sql or 'WITH ranked AS' in sql:
            joins.append(sql)
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(db.conn, 'execute', measured)
    with db.read_tx():
        result = db.ltf_projection_bundle(instrument_id, moment+100, live=True)
        assert result == initial
        result['scenarios_by_obs'][sc.observation_id][0].state = 'caller mutation'
        assert db.ltf_projection_bundle(instrument_id, moment+200, live=True) == initial
        assert not joins
    db.update_ltf_scenario(sc.id, state='cancelled')
    with db.read_tx():
        changed = db.ltf_projection_bundle(instrument_id, moment+300, live=True)
        assert changed['scenarios_by_obs'][sc.observation_id][0].state == 'cancelled'
        assert joins
        joins.clear()
        db.ltf_projection_bundle(instrument_id, T+H1_MS)
        assert joins, 'historical requests retain their exact cutoff'
