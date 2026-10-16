"""Separate source pagination and durable library briefs with isolated model responses."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.research.contracts import ResearchError
from app.research.library import Library, fetch_article
from app.research.store import Store, local_id
from app.research.feed_signals import FeedSignals
from app.web.api import create_app


@pytest.fixture
def workspace(db):
    settings = Settings(auth_token='flow-test', research_enabled=True, research_deribit_enabled=False)
    app = create_app(db, settings)
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer flow-test'
    return Store(db), settings, app, client


def post(store, external, kind, text='A useful publication about Bitcoin'):
    store.ingest('flow-test', {'entity_kind': 'materials', 'entity_id': external, 'revision_id': 'v1',
        'operation': 'upsert', 'change_seq': 1, 'payload': {'source_kind': kind, 'original_text': text}})
    return store.material(local_id('flow-test', 'materials', external))


def test_source_filter_applies_before_pagination(workspace):
    store, _, _, client = workspace
    for i in range(6):
        post(store, str(i), 'x' if i in (1, 4) else 'telegram')
    x = client.get('/api/research/v1/feed', params={'source_kind': 'x', 'limit': 1}).json()['data']
    assert len(x['items']) == 1 and x['has_more']
    first = x['items'][0]
    x2 = client.get('/api/research/v1/feed', params={'source_kind': 'x', 'limit': 1,
        'before': first['published_at'], 'before_id': first['id']}).json()['data']
    assert len(x2['items']) == 1 and not x2['has_more']
    assert x2['items'][0]['id'] != first['id']
    assert all(i['source_kind'] == 'x' for i in [*x['items'], *x2['items']])
    tg = client.get('/api/research/v1/feed?source_kind=telegram').json()['data']
    assert len(tg['items']) == 4
    assert client.get('/api/research/v1/feed?source_kind=article').status_code == 422


def test_tickers_combine_sources_and_preserve_post_links(workspace):
    store, _, _, client = workspace
    asset = store.create_asset({'display_symbol': 'BTC', 'name': 'Bitcoin',
        'external_ids': [{'namespace': 'flow-test', 'external_id': 'bitcoin'}]})
    for kind, direction in [('x', 'bullish'), ('telegram', 'bearish')]:
        item = post(store, kind, kind)
        FeedSignals(store).save(item['id'], {'source_revision_id': item['revision_id'],
            'primary_asset_uid': asset['asset_uid'], 'direction': direction})
    result = client.get('/api/research/v1/feed-tickers').json()['data']
    assert len(result['items']) == 1
    ticker = result['items'][0]
    assert ticker['mentions'] == 2 and set(ticker['sources']) == {'x', 'telegram'}
    assert ticker['bullish'] == ticker['bearish'] == 1
    assert all(p['id'] and p['source_kind'] in {'x', 'telegram'} for p in ticker['posts'])


def test_article_create_is_idempotent_and_edit_is_revision_checked(workspace):
    _, _, app, client = workspace
    body = {'title': 'Полезный ресёрч', 'text': 'Суть статьи и проверяемые аргументы.'}
    first = client.post('/api/research/v1/library', json=body, headers={'Idempotency-Key': 'article-one'})
    assert first.status_code == 201
    a = first.json()['data']
    assert client.post('/api/research/v1/library', json=body, headers={'Idempotency-Key': 'article-one'}).json()['data']['id'] == a['id']
    jobs = app.state.research.s.rows("SELECT id FROM research_job WHERE kind='article_analysis'")
    assert len(jobs) == 1
    changed = client.put('/api/research/v1/library/'+a['id'], json={'pinned': True, 'status': 'reread', 'expected_revision': a['revision_id']})
    assert changed.status_code == 200 and changed.json()['data']['pinned']
    assert client.put('/api/research/v1/library/'+a['id'], json={'status': 'read', 'expected_revision': a['revision_id']}).status_code == 409
    assert 'text' not in client.get('/api/research/v1/library').json()['data']['items'][0]
    assert client.get('/api/research/v1/library?status=reread').json()['data']['total'] == 1


class BriefModel:
    def __init__(self, callback=None):
        self.callback = callback; self.calls = 0

    def call(self, operation, payload, **kwargs):
        self.calls += 1
        assert operation == 'article_analysis' and 'untrusted' in kwargs['spec']['prompt']
        if self.callback:
            self.callback()
        return {'output': {'summary': 'Краткая суть статьи', 'theses': ['Аргумент автора'],
            'usefulness': ['Проверить продуктовую гипотезу'], 'risks': ['Нет независимой проверки'],
            'questions': ['Какие метрики доступны?'], 'tickers': ['BTC']}, 'usage': {'provider': 'xai', 'model': 'fixture'}}


def test_brief_is_saved_cached_and_invalidated_only_by_source_edits(workspace):
    store, settings, _, _ = workspace
    library = Library(store)
    a = library.save({'title': 'Исследование', 'text': 'Bitcoin: аргументы и данные.'})
    model = BriefModel()
    library.analyze(a['id'], settings, model)
    analyzed = library.r.get('article', a['id'])
    assert analyzed['brief']['usefulness'] == ['Проверить продуктовую гипотезу']
    assert library.analyze(a['id'], settings, model)['cached'] and model.calls == 1
    pinned = library.save({'pinned': True, 'expected_revision': analyzed['revision_id']}, a['id'])
    assert pinned['brief'] == analyzed['brief']
    edited = library.save({'text': 'Новая версия текста.', 'expected_revision': pinned['revision_id']}, a['id'])
    assert 'brief' not in edited


def test_concurrent_read_status_survives_analysis(workspace):
    store, settings, _, _ = workspace
    library = Library(store)
    a = library.save({'title': 'Исследование', 'text': 'Содержимое статьи.'})
    def mark_read():
        library.save({'status': 'read', 'expected_revision': a['revision_id']}, a['id'])
    library.analyze(a['id'], settings, BriefModel(mark_read))
    saved = library.r.get('article', a['id'])
    assert saved['status'] == 'read' and saved['brief']


def test_concurrent_source_edit_discards_old_brief(workspace):
    store, settings, _, _ = workspace
    library = Library(store)
    a = library.save({'title': 'Исследование', 'text': 'Старая версия статьи.'})
    def edit():
        library.save({'text': 'Новая версия статьи.', 'expected_revision': a['revision_id']}, a['id'])
    assert library.analyze(a['id'], settings, BriefModel(edit))['superseded']
    assert 'brief' not in library.r.get('article', a['id'])


def test_link_loader_rejects_private_dns_before_connecting(monkeypatch):
    monkeypatch.setattr('app.research.library.socket.getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ResearchError):
        fetch_article('https://public-looking.example/article')


def test_worker_requeues_article_if_source_changed_during_analysis(workspace, monkeypatch):
    store, _, app, client = workspace
    a=client.post('/api/research/v1/library',json={'title':'Статья','text':'Текст статьи.'}).json()['data']
    runner=app.state.research
    monkeypatch.setattr(Library,'analyze',lambda *args:{'superseded':True})
    result=runner._execute_step(store.clock())
    assert result['status']=='pending' and result['attempts']==0
    assert result['kind']=='article_analysis'
    job_id=result['id']
    monkeypatch.setattr(Library,'analyze',lambda *args:{'status':'succeeded','id':a['id']})
    store.execute('UPDATE research_job SET next_attempt_at=0 WHERE id=?',(job_id,))
    assert runner._execute_step(store.clock())['status']=='succeeded'


def test_library_requires_auth_and_nonempty_material(workspace):
    _, _, app, client = workspace
    assert client.post('/api/research/v1/library', json={'title': 'Пусто'}).status_code == 422
    unauth = TestClient(app)
    assert unauth.get('/api/research/v1/library').status_code == 401
