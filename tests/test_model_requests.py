"""Model requests run independently of retired budgets and local rate caps."""
import httpx
import pytest
from app.research.contracts import ResearchError
from app.research.model import Model,terminal_settings
from app.research.store import Store

SPEC={'prompt':'Return JSON','schema':{'name':'test','schema':{}}}


def test_old_budget_usage_and_hourly_count_never_block_requests(db):
    store=Store(db);calls=[]
    # Historical reservations and an old running call must not gate new requests.
    for index in range(60):
        store.execute("INSERT INTO research_model_usage VALUES(?,?,?,'running','999',NULL,NULL,'{}')",
                      ('old-'+str(index),store.clock(),'telegram_analysis'))
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'cost_in_usd_ticks':50000000}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model=Model(store,{'provider':'xai','key':'fixture','cheap':'fixture','strong':'fixture',
            'daily_usd':'0','reserve_usd':'NaN','input_per_million':'invalid'},client)
        for _ in range(65):
            result=model.call('telegram_analysis',{},spec=SPEC)
            assert not any('usd' in key or 'cost' in key for key in result['usage'])
        assert len(calls)==65
        assert model.status()['calls_24h']==125
        assert not any('usd' in key or 'budget' in key or key.startswith('max_') for key in model.status())
        assert store.one("SELECT COUNT(*) AS n FROM research_model_usage WHERE status='succeeded' AND actual_usd IS NULL AND reserved_usd='0'")['n']==65


def test_model_credentials_remain_required(db):
    model=Model(Store(db),{'provider':'xai','cheap':'fixture'})
    with pytest.raises(ResearchError) as error:model.call('telegram_analysis',{},spec=SPEC)
    assert error.value.code=='model_not_configured'
    assert model.status()['calls_24h']==0


def test_retired_env_and_saved_money_settings_are_ignored(db,monkeypatch):
    monkeypatch.setenv('RESEARCH_MODEL_DAILY_USD','0')
    monkeypatch.setenv('RESEARCH_MODEL_MAX_CALL_USD','invalid')
    monkeypatch.setenv('RESEARCH_MODEL_INPUT_USD_PER_MILLION','NaN')
    assert not any('usd' in key or 'million' in key for key in terminal_settings())
    store=Store(db)
    store.set_state('model-settings',{'provider':'xai','cheap':'fixture','strong':'fixture','daily_usd':'0','reserve_usd':'invalid'})
    assert 'daily_usd' not in Model(store).config
    Model(store).save_settings({'daily_usd':'NaN','reserve_usd':'invalid'})
    assert store.state('model-settings')=={'provider':'xai','cheap':'fixture','strong':'fixture'}
