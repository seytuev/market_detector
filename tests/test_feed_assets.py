"""Resolve an xAI ticker into a canonical form selection, without another model call."""
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.research.contracts import ResearchError
from app.research.feed_assets import FeedAssets
from app.research.feed_classifier import FeedClassifier
from app.research.feed_signals import FeedSignals
from app.research.store import Store
from app.web.api import create_app
from tests.test_feed_classifier import source


def classified(store, symbol='GRT', asset_uid=None):
    mid, rev = source(store, 'telegram', body='Hope for growth in GRT if Bitcoin is strong.')
    cache(store, rev, symbol, asset_uid)
    return mid, rev


def cache(store, rev, symbol='GRT', asset_uid=None):
    FeedClassifier(store, Settings(), model=object()).write(rev, 'succeeded', {
        'symbol': symbol, 'primary_asset_uid': asset_uid, 'direction': 'bullish',
        'origin': 'xai', 'confidence': 'low', 'reason_ru': 'Надежда на рост GRT.'})


def asset(store, symbol='GRT', name='The Graph', external='grt'):
    return store.create_asset({'display_symbol': symbol, 'name': name,
        'external_ids': [{'namespace': 'fixture', 'external_id': external}]})


@pytest.fixture
def resolver(db):
    store, calls, replies = Store(db), [], []
    def response(request):
        calls.append(request)
        assert request.url.host == 'data-api.binance.vision'
        assert request.url.params['symbol'] == 'GRTUSDT'
        assert not db.conn.in_transaction  # remote I/O never holds a write transaction
        return httpx.Response(200, json=replies.pop(0) if replies else {'symbols': [{
            'symbol': 'GRTUSDT', 'baseAsset': 'GRT', 'quoteAsset': 'USDT',
            'status': 'TRADING', 'isSpotTradingAllowed': True}]})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        yield store, FeedAssets(store, client), calls, replies


def test_missing_grt_is_verified_once_and_cached_result_resolves_later(resolver):
    s, service, calls, _ = resolver
    mid, rev = classified(s)
    instruments_before = len(s.db.get_instruments())
    a = service.resolve(mid, rev)
    assert a['display_symbol'] == a['name'] == 'GRT'
    assert a['external_ids'][0]['namespace'] == 'binance:spot'
    assert a['external_ids'][0]['external_id'] == 'GRTUSDT'
    assert not a['instrument_links']
    assert len(s.db.get_instruments()) == instruments_before
    assert service.resolve(mid, rev)['asset_uid'] == a['asset_uid']
    assert len(calls) == 1
    item = s.material(mid)
    FeedSignals(s).attach([item])
    c = item['classification']
    assert c['primary_asset_uid'] == a['asset_uid'] and c['asset_name'] == 'GRT'
    assert c['origin'] == 'xai' and c['direction'] == 'bullish'
    assert s.one('SELECT COUNT(*) AS n FROM research_model_usage')['n'] == 0
    assert s.one("SELECT COUNT(*) AS n FROM research_object WHERE kind='feed_classification'")['n'] == 0


def test_existing_asset_populates_name_even_when_model_cached_null_uid(resolver):
    s, service, calls, _ = resolver
    mid, rev = classified(s)
    a = asset(s)
    assert service.resolve(mid, rev)['asset_uid'] == a['asset_uid']
    item = s.material(mid)
    FeedSignals(s).attach([item])
    assert item['classification']['asset_name'] == 'The Graph'
    assert item['classification']['primary_asset_uid'] == a['asset_uid']
    assert calls == []


def test_ambiguous_ticker_requires_manual_choice_but_explicit_identity_is_kept(resolver):
    s, service, calls, _ = resolver
    a = asset(s)
    asset(s, name='Another GRT', external='other')
    mid, rev = classified(s)
    with pytest.raises(ResearchError) as error:
        service.resolve(mid, rev)
    assert error.value.status == 409
    cache(s, rev, asset_uid=a['asset_uid'])
    assert service.resolve(mid, rev)['asset_uid'] == a['asset_uid']
    assert calls == []


def test_manual_correction_has_priority(resolver):
    s, service, calls, _ = resolver
    mid, rev = classified(s)
    a = asset(s, 'SOL', 'Solana', 'sol')
    FeedSignals(s).save(mid, {'source_revision_id': rev, 'primary_asset_uid': a['asset_uid'], 'direction': 'bearish'})
    assert service.resolve(mid, rev)['asset_uid'] == a['asset_uid']
    item = s.material(mid)
    FeedSignals(s).attach([item])
    assert item['classification']['asset_name'] == 'Solana'
    assert item['classification']['direction'] == 'bearish'
    assert calls == []


@pytest.mark.parametrize('reply', [{}, {'symbols': [{'symbol': 'GRTUSDT', 'baseAsset': 'OTHER'}]},
    {'symbols': [{'symbol': 'GRTUSDT', 'baseAsset': 'GRT', 'quoteAsset': 'USDT', 'status': 'BREAK', 'isSpotTradingAllowed': True}]},
    {'symbols': None}, []])
def test_unverified_pair_is_not_created_and_failure_is_cached(resolver, reply):
    s, service, calls, replies = resolver
    mid, rev = classified(s)
    replies.append(reply)
    for _ in range(2):
        with pytest.raises(ResearchError) as error:
            service.resolve(mid, rev)
        assert error.value.code == 'asset_lookup_unavailable'
    assert len(calls) == 1
    assert s.assets('GRT')['items'] == []


def test_revision_and_ticker_validation_happen_before_lookup(resolver):
    s, service, calls, _ = resolver
    mid, rev = classified(s, symbol=None)
    with pytest.raises(ResearchError) as error:
        service.resolve(mid, 'stale-revision')
    assert error.value.status == 409
    with pytest.raises(ResearchError) as error:
        service.resolve(mid, rev)
    assert error.value.code == 'ticker_required'
    cache(s, rev, symbol='../../invalid')
    with pytest.raises(ResearchError):
        service.resolve(mid, rev)
    assert calls == []


def test_resolve_api_auth_origin_and_feed_detail(resolver, monkeypatch):
    s, service, calls, _ = resolver
    monkeypatch.setattr('app.research.feed_assets.FeedAssets', lambda store: service)
    mid, rev = classified(s)
    client = TestClient(create_app(s.db, Settings(auth_token='feed-assets-test', research_enabled=True)))
    path = '/api/research/v1/feed/' + mid
    headers = {'Authorization': 'Bearer feed-assets-test'}
    body = {'source_revision_id': rev}
    assert client.post(path + '/resolve-asset', json=body).status_code == 401
    assert client.post(path + '/resolve-asset', json=body, headers={**headers, 'Origin': 'https://evil.test'}).status_code == 403
    reply = client.post(path + '/resolve-asset', json=body, headers=headers)
    assert reply.status_code == 200, reply.text
    c = client.get(path, headers=headers).json()['data']['classification']
    assert c['primary_asset_uid'] == reply.json()['data']['asset_uid']
    assert c['asset_name'] == 'GRT' and c['origin'] == 'xai'
    assert len(calls) == 1
