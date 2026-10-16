"""Shared model settings persist without exposing or reusing another provider's key."""
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.research.model import Model
from app.research.store import Store
from app.web.api import create_app


def test_model_settings_persist_and_select_credentials(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','private-openai')
    monkeypatch.setenv('XAI_API_KEY','private-xai')
    store=Store(db)
    value={'provider':'openai','cheap':'test-small','strong':'test-large','daily_usd':'5','reserve_usd':'0.1'}
    client=TestClient(create_app(db,Settings(auth_token='test',research_enabled=True)))
    headers={'Authorization':'Bearer test'}
    assert client.put('/api/research/v1/model/settings',json=value).status_code==401
    assert client.put('/api/research/v1/model/settings',json=value,headers={**headers,'Origin':'https://evil.test'}).status_code==403
    response=client.put('/api/research/v1/model/settings',json=value,headers=headers)
    assert response.status_code==200
    assert 'private-' not in response.text
    model=Model(store)
    assert model.config['key']=='private-openai'
    assert model.config['strong']=='test-large'
    assert model.status()['remaining_usd']=='5'
    assert client.get('/api/research/v1/model/settings',headers=headers).json()['data']['model']=='test-small'
    value.update(provider='xai',daily_usd='0',reserve_usd='0')
    assert client.put('/api/research/v1/model/settings',json=value,headers=headers).status_code==200
    assert Model(store).config['key']=='private-xai'
    assert not Model(store).status()['automatic_spending_enabled']


@pytest.mark.parametrize('patch',[{'provider':'unknown'},{'provider':[]},{'daily_usd':'NaN'},{'daily_usd':'-1'},{'reserve_usd':'6'},{'cheap':''},{'key':'secret'}])
def test_invalid_settings_leave_saved_configuration_unchanged(db,patch):
    store=Store(db)
    body={'provider':'xai','cheap':'test','strong':'test','daily_usd':'5','reserve_usd':'0.1'}
    Model(store).save_settings(body)
    from app.research.contracts import ResearchError
    with pytest.raises(ResearchError):Model(store).save_settings({**body,**patch})
    assert store.state('model-settings')==body
