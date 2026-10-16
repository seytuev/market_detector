"""Credential lifecycle, server-side use, authorization and non-disclosure."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.research.accounts import XAccounts
from app.research.credentials import Credentials
from app.research.model import Model
from app.research.store import Store
from app.web.api import create_app


@pytest.fixture
def credential_api(db,monkeypatch):
    for key in ('X_BEARER_TOKEN','XAI_API_KEY','XAI','OPENAI_API_KEY','ANTHROPIC_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('RESEARCH_TERMINAL_SETTINGS_DIR','missing-test-settings')
    return TestClient(create_app(db,Settings(auth_token='test',research_enabled=True))),Store(db),{'Authorization':'Bearer test'}


def test_credentials_lifecycle_auth_and_no_disclosure(credential_api):
    client,store,headers=credential_api
    for name in ('x','xai','openai','anthropic'):
        path='/api/research/v1/credentials/'+name;secret='fixture-private-'+name
        assert client.put(path,json={'value':secret}).status_code==401
        assert client.put(path,json={'value':secret},headers={**headers,'Origin':'https://evil.test'}).status_code==403
        assert client.delete(path,headers={**headers,'Origin':'https://evil.test'}).status_code==403
        response=client.put(path,json={'value':secret},headers=headers)
        assert response.status_code==200
        assert response.json()['data']=={'configured':True,'source':'settings'}
        assert secret not in response.text
        assert Credentials(Store(store.db)).get(name)==secret
    for path in ('/credentials','/x-accounts','/config','/model/settings','/workbench/config','/telegram'):
        response=client.get('/api/research/v1'+path,headers=headers)
        assert response.status_code==200
        assert 'fixture-private-' not in response.text
    for table in ('research_audit','research_idempotency'):
        # Audit and retry caches must never contain secret values.
        if store.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)):
            assert 'fixture-private-' not in json.dumps(store.rows('SELECT * FROM '+table))
    for name in ('x','xai','openai','anthropic'):
        response=client.delete('/api/research/v1/credentials/'+name,headers=headers)
        assert response.status_code==200 and not response.json()['data']['configured']
        assert Credentials(store).get(name)==''


def test_model_and_x_use_saved_credentials_and_remove_disables_env(credential_api,monkeypatch):
    client,store,headers=credential_api
    monkeypatch.setenv('X_BEARER_TOKEN','env-x')
    monkeypatch.setenv('OPENAI_API_KEY','env-openai')
    assert XAccounts(store).token()=='env-x'
    assert client.get('/api/research/v1/credentials',headers=headers).json()['data']['items']['openai']['source']=='environment'
    for name in ('x','openai'):
        assert client.put('/api/research/v1/credentials/'+name,json={'value':'saved-'+name},headers=headers).status_code==200
    Model(store).save_settings({'provider':'openai','cheap':'test','strong':'test','daily_usd':'1','reserve_usd':'0.1'})
    assert Model(Store(store.db)).config['key']=='saved-openai'
    assert Model(store).status()['credential_source']=='settings'
    assert XAccounts(Store(store.db)).token()=='saved-x'
    for name in ('x','openai'):
        client.delete('/api/research/v1/credentials/'+name,headers=headers)
    assert not XAccounts(store).configured()
    assert not Model(store).status()['configured']


@pytest.mark.parametrize('value',['','   ','a\nb','a b','a'*8193,[],None,{'secret':'invalid'}])
def test_invalid_credentials_do_not_echo_input(credential_api,value):
    client,store,headers=credential_api
    response=client.put('/api/research/v1/credentials/x',json={'value':value},headers=headers)
    assert response.status_code==422
    assert not Credentials(store).get('x')
    assert not store.state('private-credential:x')


def test_saved_x_token_sent_to_collector(credential_api,monkeypatch):
    _,store,_=credential_api
    Credentials(store).save('x',{'value':'saved-private-x'})
    accounts=XAccounts(store);accounts.save({'handle':'example'})
    seen=[]
    def respond(request):
        seen.append(request.headers['Authorization'])
        if '/by/username/' in request.url.path:return httpx.Response(200,json={'data':{'id':'123'}})
        return httpx.Response(200,json={'meta':{}})
    original=httpx.Client
    monkeypatch.setattr('app.research.accounts.httpx.Client',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(respond)))
    assert accounts.collect('example')['new_posts']==0
    assert seen==['Bearer saved-private-x','Bearer saved-private-x']
