"""Regression coverage for provider billing and waking deferred research work."""
from decimal import Decimal
import httpx
import pytest

from app.config import Settings
from app.research.contracts import ResearchError
from app.research.jobs import ResearchRunner
from app.research.model import Model
from app.research.store import Store


def configured(store, usage, content='{}', **changes):
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}], 'usage':usage})
    client=httpx.Client(transport=httpx.MockTransport(respond))
    config={'provider':'xai','key':'fixture','cheap':'fixture-model','strong':'fixture-model',
            'daily_usd':'0.51','reserve_usd':'0.5',**changes}
    return Model(store,config,client),calls,client


def test_xai_billed_cost_releases_reserve_for_next_post(db):
    store=Store(db)
    model,calls,client=configured(store,{'prompt_tokens':2000,'completion_tokens':1000,'cost_in_usd_ticks':50_000_000},
                                  input_per_million='100',output_per_million='100')
    try:
        for _ in range(3):
            result=model.call('telegram_analysis',{},spec={'prompt':'Return JSON','schema':{'name':'test','schema':{}}})
            assert result['usage']['actual_usd']=='0.005'
        assert len(calls)==3
        assert Decimal(model.status()['accounted_usd'])==Decimal('0.015')
        with pytest.raises(ResearchError) as error:
            model.call('telegram_analysis',{})
        assert error.value.code=='budget_blocked' and len(calls)==3
    finally:
        client.close()


@pytest.mark.parametrize('ticks',[None,'NaN','Infinity',-1,'invalid','0.5'])
def test_missing_or_invalid_cost_keeps_conservative_reservation(db,ticks):
    store=Store(db)
    model,_,client=configured(store,{'cost_in_usd_ticks':ticks})
    try:
        model.call('telegram_analysis',{},spec={'prompt':'Return JSON','schema':{'name':'test','schema':{}}})
        assert model.status()['accounted_usd']=='0.5'
    finally:
        client.close()


def test_zero_cost_is_authoritative_and_failed_json_keeps_reported_charge(db):
    store=Store(db)
    model,_,client=configured(store,{'cost_in_usd_ticks':0})
    try:
        model.call('telegram_analysis',{},spec={'prompt':'Return JSON','schema':{'name':'test','schema':{}}})
        assert Decimal(model.status()['accounted_usd'])==0
    finally:
        client.close()
    model,_,client=configured(store,{'cost_in_usd_ticks':50_000_000},content='invalid JSON')
    try:
        with pytest.raises(ResearchError):
            model.call('telegram_analysis',{},spec={'prompt':'Return JSON','schema':{'name':'test','schema':{}}})
        assert Decimal(model.status()['accounted_usd'])==Decimal('0.005')
        assert store.one("SELECT actual_usd FROM research_model_usage WHERE status='error'")['actual_usd']=='0.005'
    finally:
        client.close()


def test_tariff_fallback_and_other_provider_do_not_use_xai_ticks(db):
    model=Model(Store(db),{'provider':'openai','input_per_million':'1.25','output_per_million':'2.5'})
    assert Decimal(model.actual_cost({'prompt_tokens':2000,'completion_tokens':1000,'cost_in_usd_ticks':1}))==Decimal('0.005')
    model.config['provider']='xai'
    assert Decimal(model.actual_cost({'prompt_tokens':2000,'completion_tokens':1000}))==Decimal('0.005')


@pytest.mark.parametrize('daily,reserve,wake',[('20','0.005',True),('0','0',False),('20','0',False)])
def test_saving_budget_wakes_only_pending_configuration_waits(db,daily,reserve,wake):
    store=Store(db,clock=lambda:1_000_000)
    runner=ResearchRunner(db,Settings(research_enabled=True))
    jobs={}
    for code in ('budget_blocked','model_not_configured','model_rate_limited','model_error'):
        job=runner.enqueue('telegram_analysis',{'revision_id':code},key=code)
        store.execute("UPDATE research_job SET error_code=?,next_attempt_at=9999999 WHERE id=?",(code,job['id']))
        jobs[code]=job['id']
    for state in ('running','failed','cancelled','succeeded'):
        job=runner.enqueue('telegram_analysis',key=state)
        store.execute("UPDATE research_job SET status=?,error_code='budget_blocked',next_attempt_at=9999999,lease_until=9999999 WHERE id=?",(state,job['id']))
        jobs[state]=job['id']
    Model(store).save_settings({'provider':'xai','cheap':'fixture','strong':'fixture','daily_usd':daily,'reserve_usd':reserve})
    for key,identifier in jobs.items():
        row=store.one('SELECT * FROM research_job WHERE id=?',(identifier,))
        assert row['next_attempt_at']==(store.clock() if wake and key in ('budget_blocked','model_not_configured') else 9999999)
        if key=='running':
            assert row['status']=='running' and row['lease_until']==9999999
