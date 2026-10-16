"""Feed decisions persist independently of model availability and post revisions."""
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.research.contracts import ResearchError
from app.research.feed_signals import FeedSignals
from app.research.store import Store
from app.web.api import create_app
from tests.test_telegram_feed import post


@pytest.fixture
def signals(db):
    store = Store(db)
    asset = store.create_asset({'display_symbol': 'BTC', 'name': 'Bitcoin',
        'external_ids': [{'namespace': 'fixture', 'external_id': 'bitcoin'}]})
    return store, FeedSignals(store), asset


def test_classification_survives_new_service_instance_and_keeps_history(signals):
    s, service, asset = signals
    mid, rev = post(s)
    body = {'primary_asset_uid': asset['asset_uid'], 'direction': 'bearish', 'source_revision_id': rev}
    first = service.save(mid, body)
    item = s.material(mid)
    FeedSignals(s).attach([item])
    assert item['classification']['direction'] == 'bearish'
    assert item['classification']['symbol'] == 'BTC'
    assert item['classification']['origin'] == 'manual'
    with pytest.raises(ResearchError, match='Классификация изменена'):
        service.save(mid, {**body, 'direction': 'bullish'})
    second = service.save(mid, {**body, 'direction': 'bullish', 'expected_revision': first['revision_id']})
    assert second['parent_revision_id'] == first['revision_id']
    assert s.one('SELECT COUNT(*) AS n FROM research_revision WHERE object_id=?', (first['id'],))['n'] == 2


def test_edited_post_requires_new_decision(signals):
    s, service, asset = signals
    mid, rev = post(s)
    body = {'primary_asset_uid': asset['asset_uid'], 'direction': 'bullish', 'source_revision_id': rev}
    service.save(mid, body)
    _, new_rev = post(s, text='Bitcoin launch was cancelled.', seq=2)
    with pytest.raises(ResearchError) as error:
        service.save(mid, body)
    assert error.value.status == 409
    item = s.material(mid)
    assert item['revision_id'] == new_rev
    service.attach([item])
    assert item['classification']['direction'] is None


def test_auto_direction_and_unique_ticker_ambiguity(signals):
    s, service, asset = signals
    mid, _ = post(s)
    item = s.material(mid)
    item['telegram'] = {'analysis': {'sentiment': 'bearish', 'tickers': [{'symbol': 'BTC', 'role': 'главный'}]}}
    service.attach([item])
    assert item['classification']['primary_asset_uid'] == asset['asset_uid']
    assert item['classification']['direction'] == 'bearish'
    s.create_asset({'display_symbol': 'BTC', 'name': 'Different token',
        'external_ids': [{'namespace': 'fixture', 'external_id': 'other-bitcoin'}]})
    service.attach([item])
    assert item['classification']['primary_asset_uid'] is None
    item['telegram']['analysis']['sentiment'] = 'neutral'
    service.attach([item])
    assert item['classification']['direction'] is None


@pytest.mark.parametrize('direction', ['neutral', 'watch', '', None])
def test_only_binary_decisions_can_be_saved(signals, direction):
    s, service, asset = signals
    mid, rev = post(s)
    with pytest.raises(ResearchError):
        service.save(mid, {'primary_asset_uid': asset['asset_uid'], 'direction': direction, 'source_revision_id': rev})


def test_api_auth_origin_idempotency_and_x_posts(signals):
    s, _, asset = signals
    s.ingest('fixture', {'entity_kind': 'materials', 'entity_id': 'tweet', 'revision_id': 'v1',
        'operation': 'upsert', 'change_seq': 1, 'payload': {'source_kind': 'x', 'original_text': 'BTC launches a product'}})
    client = TestClient(create_app(s.db, Settings(auth_token='feed-test', research_enabled=True)))
    headers = {'Authorization': 'Bearer feed-test', 'Idempotency-Key': 'classification-one'}
    item = client.get('/api/research/v1/feed', headers=headers).json()['data']['items'][0]
    path = '/api/research/v1/feed/' + item['id'] + '/classification'
    body = {'source_revision_id': item['revision_id'], 'primary_asset_uid': asset['asset_uid'], 'direction': 'bullish'}
    assert client.put(path, json=body).status_code == 401
    assert client.put(path, json=body, headers={**headers, 'Origin': 'https://evil.test'}).status_code == 403
    first = client.put(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert client.put(path, json=body, headers=headers).json()['data']['revision_id'] == first.json()['data']['revision_id']
    assert client.get('/api/research/v1/feed', headers=headers).json()['data']['items'][0]['classification']['direction'] == 'bullish'
    detail = client.get('/api/research/v1/feed/' + item['id'], headers=headers)
    assert detail.status_code == 200
    assert detail.json()['data']['classification']['symbol'] == 'BTC'
    assert client.get('/ideas.html').status_code == 200
