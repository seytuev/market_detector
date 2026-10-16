"""Isolated information workspace; every model request uses a local HTTP stub."""
import json
import httpx
from tests.feed_workspace_fixture_server import app, store, settings
from tests.telegram_fixture_server import response as telegram_response
from app.research.model import Model
from app.research.library import Library
import app.research.feed_classifier as classifier_module

settings.research_feed_classification_enabled = False


def response(request):
    body = json.loads(request.content)
    if body.get('response_format', {}).get('json_schema', {}).get('name') != 'article_brief':
        return telegram_response(request)
    return httpx.Response(200, json={'id': 'article-fixture', 'choices': [{'message': {'content': json.dumps({
        'summary': 'Исследование объясняет, как оценивать активность сети и отделять спрос от краткосрочного шума.',
        'theses': ['Рост числа транзакций сам по себе не подтверждает устойчивый спрос.', 'Нужны данные о комиссиях и удержании пользователей.'],
        'usefulness': ['Добавить удержание пользователей к критериям наблюдения за ETH.', 'Сравнить динамику комиссий с активностью приложений.'],
        'risks': ['Автор не учитывает субсидируемую активность.'],
        'questions': ['Растёт ли активность после окончания стимулов?'], 'tickers': ['ETH']}, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 200, 'completion_tokens': 100}})


client = httpx.Client(transport=httpx.MockTransport(response))
classifier_module.Model = lambda s, config=None: Model(s, {'provider': 'xai', 'key': 'fixture-only',
    'cheap': 'fixture-grok', 'strong': 'fixture-grok'}, client)
article = Library(store).save({'title': 'Как оценивать реальный спрос на сеть',
    'text': 'Ethereum: рост числа транзакций не всегда означает устойчивый спрос. Проверяйте комиссии, удержание пользователей и активность приложений.'})
Library(store).analyze(article['id'], settings)
