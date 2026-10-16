"""Telegram pipeline: real imported algorithms, mocked provider, isolated database."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.models import now_ms
from app.research.store import Store
from app.research.contracts import digest,encode,ResearchError
from app.research.model import Model
from app.research.jobs import ResearchRunner
from app.research.telegram import TelegramFeed,DAY
from app.web.api import create_app


def analysis(**changes):
    return {'title_ru':'Bitcoin запускает сеть','summary_ru':'Bitcoin объявил запуск тестовой сети.',
        'what_happened_ru':'Команда объявила запуск тестовой сети.','why_matters_ru':'Пользователи смогут проверить продукт.',
        'translation_ru':'Bitcoin объявил запуск тестовой сети. Подробности на сайте.',
        'check_ru':['Проверить оригинальный анонс'],'tickers':[{'symbol':'BTC','name':'Bitcoin','role':'главный','type':'crypto'}],
        'entities':[{'name':'Bitcoin','kind':'project','action':'запуск'}],'category':'product','sentiment':'neutral','impact':4,
        'event':None,'x_links':[],'unclear':[],'confidence':'medium','media':[],**changes}


@pytest.fixture
def pipeline(db):
    clock=[now_ms()-10000];s=Store(db,clock=lambda:clock[0]);calls=[];outputs=[]
    def respond(request):
        body=json.loads(request.content);calls.append(body)
        value=outputs.pop(0) if outputs else analysis()
        return httpx.Response(200,json={'id':'fixture-'+str(len(calls)),'choices':[{'message':{'content':json.dumps(value)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':80}})
    client=httpx.Client(transport=httpx.MockTransport(respond))
    config={'provider':'xai','key':'fixture-private','cheap':'fixture-model','strong':'fixture-model','daily_usd':'5','reserve_usd':'0.1'}
    model=Model(s,config,client);settings=Settings(auth_token='test',research_enabled=True)
    feed=TelegramFeed(s,settings,model)
    yield s,clock,feed,model,calls,outputs,settings
    client.close()


def post(s,channel='alpha',number=1,text='Bitcoin announced a testnet launch. https://example.org/news',seq=1,published=None):
    external=f'{channel}:{number}';payload={'source_kind':'telegram','source_id':channel,'author':channel,
        'original_text':text,'published_at':published or s.clock()-1000,'canonical_url':f'https://t.me/{channel}/{number}',
        'links':['https://example.org/news']}
    s.ingest('telegram-test',{'entity_kind':'materials','entity_id':external,'revision_id':digest(payload),'operation':'upsert','change_seq':seq,'payload':payload})
    from app.research.store import local_id
    mid=local_id('telegram-test','materials',external)
    return mid,s.one('SELECT current_revision_id FROM research_material WHERE id=?',(mid,))['current_revision_id']


def test_analysis_translation_cache_revision_and_context_storage(pipeline):
    s,clock,feed,model,calls,outputs,cfg=pipeline
    mid,rev=post(s);feed.analyze(mid,rev);feed.analyze(mid,rev)
    assert len(calls)==1
    assert calls[0]['response_format']['json_schema']['name']=='telegram_analysis'
    assert 'translation_ru' in calls[0]['response_format']['json_schema']['schema']['required']
    assert 'untrusted evidence' in calls[0]['messages'][0]['content']
    saved=json.loads(s.one('SELECT payload FROM research_telegram_analysis')['payload'])
    assert saved['analysis']['translation_ru']==analysis()['translation_ru']
    assert saved['source_revision_id']==rev and not saved['links_fetched']
    assert s.material(mid)['analyses'][0]['claims']==[]
    clock[0]+=1000
    mid,new=post(s,text='Bitcoin corrected the launch announcement.',seq=2)
    assert feed.analyze(mid,rev)['superseded']
    feed.analyze(mid,new)
    assert len(calls)==2 and s.one('SELECT COUNT(*) AS n FROM research_telegram_analysis')['n']==2
    assert feed.stories()['analyzed']==1


def test_bad_nested_output_never_becomes_analysis(pipeline):
    s,_,feed,_,calls,outputs,_=pipeline;mid,rev=post(s)
    outputs.append(analysis(tickers=[{'symbol':'BTC','name':'Bitcoin','role':'made_up','type':'crypto'}]))
    with pytest.raises(ResearchError):feed.analyze(mid,rev)
    assert s.one('SELECT status FROM research_telegram_analysis')['status']=='error'
    assert s.one('SELECT COUNT(*) AS n FROM research_analysis')['n']==0
    assert feed.stories()['items']==[]


def test_stories_merge_keep_sources_and_separate_unrelated_posts(pipeline):
    s,_,feed,_,_,outputs,_=pipeline
    for channel in ['alpha','bravo']:
        mid,rev=post(s,channel);feed.analyze(mid,rev)
    mid,rev=post(s,'charlie',text='A different announcement')
    # No shared link/ticker/entity: original algorithm must keep this separate.
    s.execute("UPDATE research_material_revision SET payload=? WHERE revision_id=?",(encode({**s.material(mid),'links':['https://different.test/other']}),rev))
    outputs.append(analysis(title_ru='Другая компания',summary_ru='Другой проект',tickers=[],entities=[]));feed.analyze(mid,rev)
    view=feed.stories();assert len(view['items'])==2
    story=next(x for x in view['items'] if len(x['posts'])==2)
    assert set(story['channels'])=={'alpha','bravo'}
    assert all(p['canonical_url'].startswith('https://t.me/') for p in story['posts'])
    assert story['priority']['score']>0


def test_digest_sources_heat_cache_and_edit_invalidation(pipeline):
    s,clock,feed,_,calls,outputs,_=pipeline
    for channel in ['alpha','bravo']:
        mid,rev=post(s,channel);feed.analyze(mid,rev)
    story=feed.stories()['items'][0]
    def result(story_id):return {'headline_ru':'Главное за сутки','bullets':[{'text_ru':'Bitcoin запустил тестовую сеть','story_id':story_id,'tickers':['BTC']}],'funds_ru':[],'watch_ru':['Проверить анонс']}
    outputs.append(result(story['id']));feed.digest();feed.digest()
    assert len(calls)==3
    saved=feed.latest_digest();assert len(saved['input_revisions'])==2
    assert saved['tickers_heat'][0]['channels']==2 and saved['tickers_heat'][0]['mentions']==2
    assert len(saved['stories'])==1
    clock[0]+=DAY+1000
    assert feed.digest()['empty']
    assert len(calls)==3


def test_digest_rejects_fabricated_source(pipeline):
    s,_,feed,_,_,outputs,_=pipeline;mid,rev=post(s);feed.analyze(mid,rev)
    outputs.append({'headline_ru':'Обзор','bullets':[{'text_ru':'Новость','story_id':'invented','tickers':[]}],'funds_ru':[],'watch_ru':[]})
    with pytest.raises(ResearchError):feed.digest()
    assert feed.latest_digest() is None


def test_budget_failure_preserves_source_and_no_paid_request(pipeline):
    s,_,feed,model,calls,_,_=pipeline;mid,rev=post(s);model.config['daily_usd']='0'
    with pytest.raises(ResearchError) as error:feed.analyze(mid,rev)
    assert error.value.code=='budget_blocked' and not calls
    assert s.material(mid)['original_text'].startswith('Bitcoin')
    assert s.one('SELECT error_code FROM research_telegram_analysis')['error_code']=='budget_blocked'


def test_queue_dedup_manual_retry_and_worker_deferred_budget(pipeline,monkeypatch):
    s,_,feed,model,calls,_,cfg=pipeline
    monkeypatch.setattr('app.research.telegram.Model',lambda store:model)
    runner=ResearchRunner(s.db,cfg);mid,rev=post(s)
    first=feed.queue(runner,mid);second=feed.queue(runner,mid)
    assert first['id']==second['id']
    model.config['daily_usd']='0'
    runner.last_schedule=runner.last_calendar=runner.last_workbench=now_ms()
    runner.step()
    job=runner.job(first['id']);assert job['status']=='pending' and job['error_code']=='budget_blocked'
    assert not calls
    model.config['daily_usd']='5'
    s.execute('UPDATE research_job SET next_attempt_at=0 WHERE id=?',(first['id'],));runner.step()
    assert runner.job(first['id'])['status']=='succeeded' and len(calls)==1
    assert feed.queue(runner,mid,manual=True) is None


def test_automatic_backlog_window_and_digest_schedule(pipeline,monkeypatch):
    s,clock,feed,model,calls,outputs,cfg=pipeline
    monkeypatch.setattr('app.research.telegram.Model',lambda store:model)
    runner=ResearchRunner(s.db,cfg)
    post(s,'oldie',published=s.clock()-8*DAY)
    mid,rev=post(s)
    feed.maintenance(runner);feed.maintenance(runner)
    assert s.one("SELECT COUNT(*) AS n FROM research_job WHERE kind='telegram_analysis'")['n']==1
    feed.analyze(mid,rev)
    feed.maintenance(runner);feed.maintenance(runner)
    assert s.one("SELECT COUNT(*) AS n FROM research_job WHERE kind='telegram_digest'")['n']==1


def test_feed_api_auth_origin_analysis_and_disabled(pipeline,monkeypatch):
    s,_,feed,model,_,_,cfg=pipeline
    monkeypatch.setattr('app.research.telegram.Model',lambda store:model)
    mid,rev=post(s);feed.analyze(mid,rev)
    client=TestClient(create_app(s.db,cfg));headers={'Authorization':'Bearer test'}
    assert client.get('/api/research/v1/telegram').status_code==401
    assert client.post('/api/research/v1/telegram/digest',headers={**headers,'Origin':'https://evil.test'}).status_code==403
    data=client.get('/api/research/v1/telegram',headers=headers).json()['data']
    assert len(data['items'])==1 and data['model']['configured']
    assert 'fixture-private' not in encode(data)
    item=client.get('/api/research/v1/feed',headers=headers).json()['data']['items'][0]
    assert item['telegram']['analysis']['translation_ru']
    off=TestClient(create_app(s.db,Settings(auth_token='test',research_enabled=True,research_telegram_analysis_enabled=False)))
    assert off.post('/api/research/v1/telegram/process',headers=headers).status_code==503

def test_edited_post_invalidates_digest_even_with_same_summary(pipeline):
    s,clock,feed,_,calls,outputs,_=pipeline
    mid,rev=post(s);feed.analyze(mid,rev)
    story=feed.stories()['items'][0]
    output={'headline_ru':'Обзор','bullets':[{'text_ru':'Анонс','story_id':story['id'],'tickers':['BTC']}],'funds_ru':[],'watch_ru':[]}
    outputs.append(output);first=feed.digest()['id']
    signature=feed.digest_input()[2]
    clock[0]+=1000
    mid,new=post(s,text='Edited original with corrected date',seq=2)
    assert not feed.stories()['items']
    feed.analyze(mid,new)
    assert feed.digest_input()[2]!=signature
    outputs.append(output);second=feed.digest()['id']
    assert first!=second and len(calls)==4
    assert feed.latest_digest()['story_sources'][story['id']]==[[mid,new]]


def test_source_truncation_is_explicit_and_payload_is_bounded(pipeline):
    s,_,feed,_,calls,outputs,_=pipeline
    mid,rev=post(s,text='x'*6100)
    outputs.append(analysis(title_ru='<script>alert(1)</script>'))
    feed.analyze(mid,rev)
    saved=decode_payload(s,rev)
    assert saved['text_truncated']
    assert len(calls[0]['messages'][1]['content'])<48000


def decode_payload(s,revision):
    return json.loads(s.one('SELECT payload FROM research_telegram_analysis WHERE revision_id=?',(revision,))['payload'])
