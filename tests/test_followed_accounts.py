import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.research.accounts import XAccounts,normalize_handle
from app.research.channels import Channels
from app.research.contracts import ResearchError
from app.research.store import Store
from app.web.api import create_app


@pytest.mark.parametrize('value,platform,expected',[
    ('@SomeChannel','telegram','somechannel'),('https://t.me/SomeChannel','telegram','somechannel'),
    ('t.me/SomeChannel/','telegram','somechannel'),('https://x.com/Example','x','example'),
    ('https://twitter.com/Example?lang=en','x','example'),('@a','x','a')])
def test_normalize(value,platform,expected):assert normalize_handle(value,platform)==expected


@pytest.mark.parametrize('value,platform',[
    ('https://evil.com/example','x'),('https://t.me/example/123','telegram'),('https://x.com/example','telegram'),
    ('https://t.me/example','x'),('name with spaces','x'),('a'*16,'x'),([], 'x')])
def test_bad_handle(value,platform):
    with pytest.raises(ResearchError):normalize_handle(value,platform)


def test_accounts_api_lifecycle(db,monkeypatch):
    monkeypatch.delenv('X_BEARER_TOKEN',raising=False)
    client=TestClient(create_app(db,Settings(auth_token='test',research_enabled=True)))
    headers={'Authorization':'Bearer test'}
    for path,handle in [('/channels','https://t.me/Example'),('/x-accounts','https://x.com/Example')]:
        assert client.post('/api/research/v1'+path,json={'handle':handle,'enabled':True}).status_code==401
        assert client.post('/api/research/v1'+path,json={'handle':handle,'enabled':True},headers={**headers,'Origin':'https://evil.com'}).status_code==403
        assert client.post('/api/research/v1'+path,json={'handle':handle,'enabled':True},headers=headers).status_code==200
        assert client.post('/api/research/v1'+path,json={'handle':'@EXAMPLE','enabled':False},headers=headers).status_code==200
        items=client.get('/api/research/v1'+path,headers=headers).json()['data']['items']
        assert len(items)==1 and items[0]['handle']=='example' and not items[0]['enabled']
        assert client.delete('/api/research/v1'+path+'/example',headers=headers).status_code==200
        assert not client.get('/api/research/v1'+path,headers=headers).json()['data']['items']
    XAccounts(Store(db)).save({'handle':'example','enabled':True})
    assert client.post('/api/research/v1/x-accounts/example/collect',headers=headers).status_code==503


def test_x_collection_pagination_dedup_and_disabled(db,monkeypatch):
    monkeypatch.setenv('X_BEARER_TOKEN','test-private')
    store=Store(db);accounts=XAccounts(store);accounts.save({'handle':'example'})
    requests=[]
    def response(request):
        requests.append(request)
        if 'by/username' in request.url.path:return httpx.Response(200,json={'data':{'id':'123'}})
        older=request.url.params.get('pagination_token')=='next'
        return httpx.Response(200,json={'data':[{'id':'10' if older else '20','text':'Test post','created_at':'2026-10-16T10:00:00Z'}],'meta':{} if older else {'next_token':'next'}})
    with httpx.Client(base_url='https://api.x.com/2/',transport=httpx.MockTransport(response)) as client:
        assert accounts.collect('example',client)['new_posts']==1
        assert not store.state('x-account:example').get('since_id')
        assert accounts.collect('example',client)['new_posts']==1
        assert store.state('x-account:example')['since_id']=='20'
        assert not store.state('x-account:example').get('pagination_token')
        assert accounts.collect('example',client)['new_posts']==0
        accounts.save({'handle':'example','enabled':False})
        with pytest.raises(ResearchError):accounts.collect('example',client)
    assert store.one("SELECT COUNT(*) AS n FROM research_material_revision")['n']==2
    assert 'test-private' not in str(accounts.list())


def test_x_rate_limit_preserves_cursor(db,monkeypatch):
    monkeypatch.setenv('X_BEARER_TOKEN','test-private');store=Store(db);accounts=XAccounts(store)
    accounts.save({'handle':'example'})
    store.set_state('x-account:example',{**store.state('x-account:example'),'user_id':'123','since_id':'10'})
    with httpx.Client(base_url='https://api.x.com/2/',transport=httpx.MockTransport(lambda r:httpx.Response(429))) as client:
        with pytest.raises(ResearchError):accounts.collect('example',client)
    state=store.state('x-account:example')
    assert state['since_id']=='10' and state['error_code']=='x_rate_limited'
