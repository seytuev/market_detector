"""Real xAI request path with deterministic provider responses and isolated storage."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.research.contracts import ResearchError
from app.research.credentials import Credentials
from app.research.feed_classifier import FeedClassifier
from app.research.feed_signals import FeedSignals
from app.research.jobs import ResearchRunner
from app.research.model import Model
from app.research.store import Store, local_id
from app.web.api import create_app


def source(s, kind='x', external='post', revision='v1', seq=1, body='Bitcoin launches a product. $BTC'):
    s.ingest('classifier-fixture', {'entity_kind': 'materials', 'entity_id': external, 'revision_id': revision,
        'operation': 'upsert', 'change_seq': seq, 'payload': {'source_kind': kind, 'original_text': body}})
    mid = local_id('classifier-fixture', 'materials', external)
    return mid, s.material(mid)['revision_id']


@pytest.fixture
def classifier(db):
    s = Store(db)
    a = s.create_asset({'display_symbol': 'BTC', 'name': 'Bitcoin',
        'external_ids': [{'namespace': 'fixture', 'external_id': 'btc'}]})
    calls, outputs = [], []
    default = {'direction': 'bullish', 'symbol': 'BTC', 'asset_uid': None,
        'confidence': 'medium', 'reason_ru': 'Запуск продукта может увеличить использование Bitcoin.'}
    def respond(request):
        calls.append(request)
        value = outputs.pop(0) if outputs else default
        if callable(value):
            value = value()
        return httpx.Response(200, json={'id': 'classifier-fixture', 'choices': [{'message': {'content': json.dumps(value)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50}})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = Model(s, {'provider': 'xai', 'key': 'fixture-private', 'cheap': 'fixture-grok', 'strong': 'fixture-grok',
        'daily_usd': '5', 'reserve_usd': '0.1'}, client)
    cfg = Settings(auth_token='classifier-test', research_enabled=True, research_deribit_enabled=False,
        research_telegram_analysis_enabled=False, research_telegram_digest_enabled=False)
    service = FeedClassifier(s, cfg, model)
    yield s, a, cfg, service, model, calls, outputs, default
    client.close()


@pytest.mark.parametrize('kind', ['x', 'telegram'])
def test_xai_classifies_each_kind_once_per_revision(classifier, kind):
    s, a, _, service, _, calls, _, _ = classifier
    mid, rev = source(s, kind)
    service.classify(mid, rev)
    assert service.classify(mid, rev) == {'cached': True}
    assert len(calls) == 1
    assert str(calls[0].url) == 'https://api.x.ai/v1/chat/completions'
    request = json.loads(calls[0].content)
    assert request['response_format']['json_schema']['strict'] is True
    assert request['max_tokens'] == 900
    assert 'untrusted evidence' in request['messages'][0]['content']
    item = s.material(mid)
    FeedSignals(s).attach([item])
    result = item['classification']
    assert result['origin'] == 'xai' and result['direction'] == 'bullish'
    assert result['symbol'] == 'BTC' and result['primary_asset_uid'] == a['asset_uid']
    assert 'fixture-private' not in json.dumps(result)
    assert s.one('SELECT COUNT(*) AS n FROM research_model_usage')['n'] == 1
    service.write(rev, 'queued', {})
    assert service.result(rev)['status'] == 'succeeded'
    source(s, kind, revision='v2', seq=2, body='Bitcoin product launch cancelled.')
    new_rev = s.material(mid)['revision_id']
    assert service.classify(mid, rev) == {'superseded': True}
    service.classify(mid, new_rev)
    assert len(calls) == 2


def test_manual_override_wins_even_when_saved_during_request(classifier):
    s, a, _, service, _, calls, outputs, default = classifier
    mid, rev = source(s)
    def reply():
        FeedSignals(s).save(mid, {'source_revision_id': rev, 'primary_asset_uid': a['asset_uid'], 'direction': 'bearish'})
        return default
    outputs.append(reply)
    service.classify(mid, rev)
    item = s.material(mid)
    FeedSignals(s).attach([item])
    assert item['classification']['direction'] == 'bearish'
    assert item['classification']['origin'] == 'manual'
    assert service.classify(mid, rev) == {'manual_override': True}
    assert len(calls) == 1


def test_no_ticker_and_ambiguous_symbol_are_not_fabricated(classifier):
    s, _, _, service, _, _, outputs, default = classifier
    s.create_asset({'display_symbol': 'BTC', 'name': 'Other Bitcoin',
        'external_ids': [{'namespace': 'fixture', 'external_id': 'other-btc'}]})
    mid, rev = source(s)
    service.classify(mid, rev)
    item = s.material(mid)
    FeedSignals(s).attach([item])
    assert item['classification']['primary_asset_uid'] is None
    mid, rev = source(s, external='no-ticker', body='Conference doors open at 10 AM.')
    outputs.append({**default, 'symbol': None, 'confidence': 'low', 'reason_ru': 'Актив и финансовый тезис не определены.'})
    service.classify(mid, rev)
    item = s.material(mid)
    FeedSignals(s).attach([item])
    assert item['classification']['symbol'] is None


def test_bad_model_output_is_not_used_and_zero_legacy_budget_does_not_block(classifier):
    s, _, _, service, model, calls, outputs, default = classifier
    mid, rev = source(s)
    outputs.append({**default, 'asset_uid': 'invented-identity'})
    with pytest.raises(ResearchError):
        service.classify(mid, rev)
    assert service.result(rev)['status'] == 'error'
    model.config['daily_usd'] = '0'
    service.classify(mid, rev)
    assert service.result(rev)['status']=='succeeded'
    assert len(calls) == 2


def test_scheduler_deduplicates_old_and_new_posts_and_runner_dispatches(classifier, monkeypatch):
    s, _, cfg, service, _, calls, _, _ = classifier
    mid, rev = source(s)
    second, _ = source(s, 'telegram', external='telegram')
    runner = ResearchRunner(s.db, cfg)
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', lambda store, settings: service)
    service.maintenance(runner)
    service.maintenance(runner)
    assert s.one("SELECT COUNT(*) AS n FROM research_job WHERE kind='feed_classification'")['n'] == 2
    runner.step()
    runner.step()
    assert len(calls) == 2
    assert service.result(rev)['status'] == 'succeeded'


def test_retry_api_auth_manual_correction_and_disabled(classifier, monkeypatch):
    s, a, cfg, service, _, _, _, _ = classifier
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', lambda store, settings: service)
    mid, rev = source(s)
    client = TestClient(create_app(s.db, cfg))
    headers = {'Authorization': 'Bearer classifier-test'}
    path = '/api/research/v1/feed/' + mid + '/classify'
    assert client.post(path).status_code == 401
    assert client.post(path, headers={**headers, 'Origin': 'https://evil.test'}).status_code == 403
    r = client.post(path, headers=headers)
    assert r.status_code == 202 and r.json()['data']['job_id']
    assert client.post(path, headers=headers).json()['data']['job_id'] == r.json()['data']['job_id']
    service.classify(mid, rev)
    manual = client.put('/api/research/v1/feed/' + mid + '/classification', headers=headers,
        json={'source_revision_id': rev, 'direction': 'bearish', 'primary_asset_uid': a['asset_uid']})
    assert manual.status_code == 200
    result = client.get('/api/research/v1/feed/' + mid, headers=headers).json()['data']['classification']
    assert result['origin'] == 'manual' and result['direction'] == 'bearish'
    cfg.research_feed_classification_enabled = False
    assert client.post(path, headers=headers).status_code == 503


def test_xai_uses_own_credential_even_with_other_provider(classifier, monkeypatch):
    s, _, cfg, _, _, _, _, _ = classifier
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('LLM_CHEAP_MODEL', 'claude-fixture')
    monkeypatch.setenv('RESEARCH_MODEL_DAILY_USD', '3')
    monkeypatch.setenv('RESEARCH_MODEL_MAX_CALL_USD', '0.2')
    Credentials(s).save('xai', {'value': 'private-xai-fixture'})
    service = FeedClassifier(s, cfg)
    assert service.model.config['provider'] == 'xai'
    assert service.model.config['key'] == 'private-xai-fixture'
    assert service.model.config['cheap'] != 'claude-fixture'
    assert 'daily_usd' not in service.model.config
    assert Model(s).config['provider'] == 'anthropic'


def test_manual_backlog_does_not_starve_unclassified_posts(classifier):
    s, a, cfg, service, _, _, _, _ = classifier
    for index in range(105):
        mid, rev = source(s, external='manual-' + str(index))
        FeedSignals(s).save(mid, {'source_revision_id': rev, 'primary_asset_uid': a['asset_uid'], 'direction': 'bullish'})
    mid, rev = source(s, external='unclassified')
    runner = ResearchRunner(s.db, cfg)
    service.maintenance(runner)
    assert service.result(rev)['status'] == 'queued'
    assert s.one("SELECT COUNT(*) AS n FROM research_job WHERE kind='feed_classification'")['n'] == 1


def test_runner_ignores_zero_legacy_budget(classifier, monkeypatch):
    s, _, cfg, service, model, calls, _, _ = classifier
    mid, rev = source(s)
    runner = ResearchRunner(s.db, cfg)
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', lambda store, settings: service)
    job = service.queue(runner, mid)
    model.config['daily_usd'] = '0'
    result = runner.step()
    assert result['id'] == job['id']
    assert result['status'] == 'succeeded'
    assert len(calls) == 1


@pytest.mark.parametrize('classifier_first',[True,False])
def test_telegram_full_analysis_and_classification_share_one_request(classifier,classifier_first):
    from app.research.telegram import TelegramFeed
    from tests.test_telegram_feed import analysis
    s,_,cfg,service,model,calls,outputs,default=classifier
    cfg.research_telegram_analysis_enabled=True
    mid,rev=source(s,'telegram')
    outputs.append(analysis(classification=default))
    feed=TelegramFeed(s,cfg,model)
    if classifier_first:service.classify(mid,rev)
    feed.analyze(mid,rev)
    service.classify(mid,rev)
    feed.analyze(mid,rev)
    assert len(calls)==1
    assert service.result(rev)['status']=='succeeded'
    assert json.loads(service.result(rev)['payload'])['direction']=='bullish'
    schema=json.loads(calls[0].content)['response_format']['json_schema']['schema']
    assert 'classification' in schema['required']


def test_existing_telegram_analysis_is_reused_without_model_call(classifier):
    from app.research.telegram import TelegramFeed
    from tests.test_telegram_feed import analysis
    s,_,cfg,service,model,calls,outputs,_=classifier
    cfg.research_telegram_analysis_enabled=True
    mid,rev=source(s,'telegram')
    outputs.append(analysis())
    TelegramFeed(s,cfg,model).analyze(mid,rev)
    service.classify(mid,rev)
    assert len(calls)==1
    assert service.result(rev)['status']=='succeeded'
