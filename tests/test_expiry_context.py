from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.research.catalysts import Catalysts, DAY
from app.research.expiry_context import expiry_context, market_expiries
from app.research.sources import deribit_rows
from app.research.store import Store
from app.web.api import create_app


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


EXPIRY = ms('2026-10-30T08:00:00')
KNOWN = ms('2026-10-20T12:00:00')


def expiry(currency='BTC', expires=EXPIRY):
    return deribit_rows([{'kind': 'option', 'expiration_timestamp': expires,
                          'instrument_name': currency + '-TEST'}], currency, expires - DAY)[0]


@pytest.mark.parametrize('at,phase,side,active', [
    ('2026-10-28T23:59:59', 'upcoming', 'neutral', False),
    ('2026-10-29T00:00:00', 'thursday', 'long', True),
    ('2026-10-30T00:00:00', 'before_08', 'short', True),
    ('2026-10-30T07:59:59', 'before_08', 'short', True),
    ('2026-10-30T08:00:00', 'after_08', 'neutral', False),
    ('2026-10-30T23:59:59', 'after_08', 'neutral', False),
    ('2026-10-31T00:00:00', 'weekend', 'neutral', False),
    ('2026-11-02T12:00:00', 'monday', 'neutral', False),
    ('2026-11-03T00:00:00', 'past', 'neutral', False),
])
def test_utc_boundaries(at, phase, side, active):
    context = expiry_context(expiry(), ms(at))
    assert (context['phase'], context['side'], context['active']) == (phase, side, active)


@pytest.mark.parametrize('date,cycle,applicable', [
    ('2026-12-25T08:00:00', 'quarterly', True),
    ('2026-10-30T08:00:00', 'monthly', True),
    ('2026-10-23T08:00:00', 'weekly', False),
    ('2026-10-29T08:00:00', 'daily', False),
    ('2026-10-30T09:00:00', 'monthly', False),
])
def test_study_scope(date, cycle, applicable):
    stamp = ms(date)
    context = expiry_context(expiry(expires=stamp), stamp - 1000)
    assert context['cycle'] == cycle
    assert context['study_applicable'] is applicable
    assert bool(context['statistics']) is applicable
    if not applicable:
        assert not context['active']


def test_eth_does_not_inherit_btc_numbers():
    context = expiry_context(expiry('ETH'), EXPIRY - DAY)
    assert context['side'] == 'neutral' and not context['active']
    assert '−0.50%' not in ' '.join(context['statistics'])
    assert '5.03%' in ' '.join(context['statistics'])


@pytest.mark.parametrize('change', [
    {'kind': 'macro'}, {'stage': 'cancelled'}, {'stage': 'denied'},
    {'review_status': 'pending'}, {'precision': 'day'}, {'sources': []},
    {'sources': [{'namespace': 'deribit', 'external_id': 'SOL:option:' + str(EXPIRY)}]},
])
def test_unrelated_and_cancelled_dates_excluded(change):
    assert expiry_context({**expiry(), **change}, EXPIRY - 1000) is None


def test_saved_rows_api_calendar_and_no_future_knowledge(db, monkeypatch):
    store = Store(db, clock=lambda: KNOWN)
    catalyst = Catalysts(store).save(expiry(), actor='deribit')
    next_expiry = ms('2026-11-27T08:00:00')
    Catalysts(store).save(expiry(expires=next_expiry), actor='deribit')
    before = ms('2026-10-30T07:00:00')
    store.clock = lambda: before
    store.set_state('deribit:BTC', {'last_success_at': before})
    context = market_expiries(store)['items'][0]
    assert context['catalyst_id'] == catalyst['id']
    assert context['phase'] == 'before_08' and context['catalog_quality'] == 'ok'
    store.clock = lambda: KNOWN - 1
    assert market_expiries(store)['items'] == []

    monkeypatch.setattr('app.web.events_api.now_ms', lambda: before)
    client = TestClient(create_app(db, Settings(auth_token='test', research_enabled=True)))
    client.app.state.research.s.clock = lambda: before
    headers = {'Authorization': 'Bearer test'}
    assert client.get('/api/events/overview').status_code == 401
    overview = client.get('/api/events/overview', headers=headers).json()
    assert overview['market'] is None
    assert overview['expiry_context']['items'][0]['phase'] == 'before_08'
    calendar = client.get('/api/events/calendar?day=2026-10-30', headers=headers).json()
    assert calendar['expiry_context']['items'][0]['phase'] == 'expiry_day'
    detail = client.get('/api/research/v1/catalysts/' + catalyst['id'], headers=headers).json()['data']
    assert detail['expiry_context']['phase'] == 'before_08'
    page = client.get('/api/research/v1/catalysts', headers=headers).json()['data']
    assert page['items'][0]['expiry_context']['phase'] == 'before_08'
    assert 'expiry_context' not in Catalysts(store).get(catalyst['id'], as_of=before)
    assert Catalysts(store).get(catalyst['id'], as_of=before)['revision_id'] == catalyst['revision_id']
    client_off = TestClient(create_app(db, Settings(auth_token='test', research_enabled=False)))
    assert client_off.get('/api/events/overview', headers=headers).json()['expiry_context']['items'] == []
