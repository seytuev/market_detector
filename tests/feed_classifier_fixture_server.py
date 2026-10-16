"""Isolated UI fixture for automatic xAI output and manual correction."""
import json
import httpx
from tests.feed_workspace_fixture_server import app, store, settings
from tests.test_feed_classifier import source
from app.research.model import Model
from app.research.feed_classifier import FeedClassifier
import app.research.feed_classifier as module


def respond(request):
    return httpx.Response(200, json={'id': 'ui-fixture', 'choices': [{'message': {'content': json.dumps({
        'direction': 'bullish', 'symbol': 'ETH', 'asset_uid': None, 'confidence': 'medium',
        'reason_ru': 'Запуск новых инструментов может повысить использование сети. Сроки требуют проверки.'})}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 60}})


client = httpx.Client(transport=httpx.MockTransport(respond))
model = Model(store, {'provider': 'xai', 'key': 'fixture-only', 'cheap': 'fixture-grok', 'strong': 'fixture-grok',
    'daily_usd': '5', 'reserve_usd': '0.1'}, client)
service = FeedClassifier(store, settings, model)
module.FeedClassifier = lambda store, settings: service
mid, rev = source(store, external='automatic-x', body='Ethereum launches new developer tools. $ETH')
service.classify(mid, rev)
