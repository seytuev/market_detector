"""Visual fixture: xAI found GRT before it existed in the asset registry."""
import httpx
from tests.feed_workspace_fixture_server import app, store, settings
from tests.test_feed_assets import cache
from app.research.store import local_id
import app.research.feed_assets as module

settings.research_feed_classification_enabled = False
store.ingest('asset-fixture', {'entity_kind': 'materials', 'entity_id': 'grt', 'revision_id': 'v1',
    'operation': 'upsert', 'change_seq': 1, 'payload': {'source_kind': 'telegram', 'author': 'easychartgo',
        'title': 'Пост о GRT: если биткоин будет к нам добр',
        'original_text': 'Надежда на рост GRT при благоприятном BTC.'}})
mid = local_id('asset-fixture', 'materials', 'grt')
cache(store, store.material(mid)['revision_id'])
client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'symbols': [{
    'symbol': 'GRTUSDT', 'baseAsset': 'GRT', 'quoteAsset': 'USDT', 'status': 'TRADING', 'isSpotTradingAllowed': True}]})))
resolver = module.FeedAssets
module.FeedAssets = lambda s: resolver(s, client)
