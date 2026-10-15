from copy import deepcopy
from datetime import datetime,timezone
import json
import math
from zipfile import ZipFile
import io
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.models import Instrument,Candle
from app.research.contracts import ResearchError,encode,digest
from app.research.store import Store
from app.research.records import Records
from app.research.market import Market,HOUR,DAY
from app.research.strength import Strength,metrics
from app.research.ideas import Ideas
from app.research.baskets import Baskets
from app.research.playbook import Playbook
from app.research.model import Model
from app.research.lab import Lab
from app.research.lab_compute import descriptive,simulate,compute
from app.research.jobs import ResearchRunner
from app.web.api import create_app

NOW=int(datetime(2026,10,14,12,tzinfo=timezone.utc).timestamp()*1000)


@pytest.fixture
def work(db):
    moment=[NOW];s=Store(db,clock=lambda:moment[0]);r=Records(s);instruments=[];assets=[]
    for symbol,external in (("BTC","bitcoin"),("ETH","ethereum")):
        a=s.create_asset({"display_symbol":symbol,"name":symbol,"external_ids":[{"namespace":"coingecko","external_id":external}]})
        i=db.upsert_instrument(Instrument(None,symbol,"binance","spot",symbol+"USDT","USDT"));s.link(i,{"asset_uid":a["asset_uid"],"reason":"Fixture identity verified"})
        instruments.append(i);assets.append(a["asset_uid"])
    return s,r,moment,instruments,assets


def bar(at,price,step=DAY):return {"open_time":at,"close_boundary":at+step,"open":price,"close":price,"high":price*1.01,"low":price*.99,"valid":True,"source":"fixture","revision":0}


def candle(i,at,price,tf="D1",step=DAY):return Candle(i,tf,at,at+step-1,price,price*1.01,price*.99,price,True,"fixture")


def idea(asset,**changes):
    return {"title":"A verifiable research thesis","primary_asset_uid":asset,"thesis":"The published product launch may attract additional demand.",
        "mechanism":"Usage can create demand for the underlying asset.","criteria":[{"type":"invalidation","description":"The launch is cancelled by the team.","evaluator":"manual"}],
        "review_due_at":NOW+DAY,"direction":"positive",**changes}


def strategy_config():
    return {"universe":{"type":"manual_list","tickers":["ETH"]},"entry":{"op":"AND","rules":[{"template":"breakout","params":{"lookback_days":2}}]},
        "exit":{"op":"OR","rules":[{"template":"take_profit","params":{"take_profit_pct":20}}]},
        "risk":{"max_position_pct":50,"max_positions":2,"stop_loss_pct":10,"time_stop":168,"max_drawdown_kill_pct":50,"cooldown_days":0},
        "costs":{"fee_bps":10,"slippage_bps":5},"params":{}}


def source(s,asset,index,at=NOW):
    ref=str(index);p={"title":"Published launch "+ref,"original_text":"The team confirms a public network launch and verified product usage.","published_at":at-1000,
        "source_kind":"telegram","author":"author","canonical_url":"https://example.test/source/"+ref,"assets":[{"entity_id":"eth","role":"subject"}]}
    s.execute("INSERT INTO research_asset_external_id VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO NOTHING",("fixture","eth",asset,at,"{}"))
    s.ingest("fixture",{"entity_kind":"materials","entity_id":ref,"revision_id":"v1","operation":"upsert","change_seq":1,"payload":p})
    row=s.one("SELECT id,current_revision_id FROM research_material WHERE provider='fixture' AND external_id=?",(ref,))
    return {"kind":"material","id":row["id"],"revision_id":row["current_revision_id"]}


def test_records_versioning_cursor_and_snapshot_integrity(work):
    s,r,m,ids,assets=work
    a=r.save("idea",{"title":"a","status":"draft"});b=r.save("idea",{"title":"b","status":"draft"});page=r.list("idea",limit=1)
    m[0]+=1;r.save("idea",{"title":"changed","status":"draft"},a["id"],a["revision_id"])
    second=r.list("idea",after=page["next_cursor"],limit=1)
    assert len({v["id"] for v in page["items"]+second["items"]})==2
    assert r.get("idea",a["id"],as_of=NOW)["title"]=="a"
    with pytest.raises(ResearchError):r.save("idea",{"title":"bad"},a["id"],a["revision_id"])
    snap=r.snapshot("test",{"frozen":[1,2]});s.execute("UPDATE research_dataset SET payload=? WHERE id=?",('{"changed":true}',snap["id"]))
    with pytest.raises(ResearchError,match="Контрольная"):r.dataset(snap["id"])


def test_strength_math_is_not_ratio_and_missing_is_null():
    a=[bar(i*DAY,100+i*2) for i in range(91)];b=[bar(i*DAY,100+i) for i in range(91)]
    result=metrics(a,b)
    assert result["return_7"]["value"]==pytest.approx((280/266-1)*100)
    assert result["excess_7"]["value"]==pytest.approx((280/266-190/183)*100)
    assert result["rs_ratio_7"]["value"]==pytest.approx(((280/266)/(190/183)-1)*100)
    assert metrics(a,a)["excess_30"]["value"]==0
    assert metrics(a,a)["correlation_60"]["value"]==pytest.approx(1)
    assert metrics(a[:-1],b)["return_7"]["value"] is None
    flat=[bar(i*DAY,100) for i in range(91)]
    assert metrics(flat,flat)["correlation_60"]["reason_codes"]==["constant_series"]


def test_strength_snapshots_sort_nulls_and_cursor(work):
    s,r,m,ids,assets=work
    for j,i in enumerate(ids):s.db.insert_candles([candle(i,NOW//DAY*DAY-(92-k)*DAY,100+k*(j+1)) for k in range(92)])
    service=Strength(s);one=service.calculate(ids);two=service.calculate(ids)
    assert one["id"]==two["id"]
    first=service.list(limit=1);second=service.list(limit=1,after=first["next_cursor"])
    assert first["items"][0]["instrument_id"]!=second["items"][0]["instrument_id"]
    with pytest.raises(ResearchError):service.list(instrument_ids=ids,asset_uids=assets)
    matrix=service.correlations(ids)
    assert len(matrix["matrix"])==4


def test_idea_lifecycle_keeps_baseline_and_does_not_touch_technical_tables(work):
    s,r,m,ids,assets=work;service=Ideas(s)
    with pytest.raises(ResearchError):service.save(idea(assets[1],status="watching",criteria=[]))
    s.db.set_quote(ids[1],100,NOW,price_kind="last_trade",event_at=NOW,received_at=NOW)
    a=service.save(idea(assets[1],status="watching",instrument_id=ids[1]))
    baseline=service.outcomes(a["id"])["items"][0]["baseline"]
    assert baseline["price"]==100 and baseline["observed_at"]>=a["available_at"]
    m[0]+=1;updated=service.save({"thesis":"A revised but independently testable product hypothesis.","expected_revision":a["revision_id"],"reason":"New evidence clarified the mechanism"},a["id"])
    assert r.get("idea",a["id"],a["revision_id"])["thesis"]==a["thesis"]
    assert service.outcomes(a["id"])["items"][0]["baseline"]==baseline
    with pytest.raises(ResearchError):service.transition(a["id"],{"status":"invalidated","expected_revision":updated["revision_id"],"reason":"Observed contradiction"})
    final=service.transition(a["id"],{"status":"invalidated","expected_revision":updated["revision_id"],"reason":"Observed contradiction","manual_decision":True})
    assert final["status"]=="invalidated" and s.one("SELECT COUNT(*) AS n FROM htf_idea")["n"]==0


def test_idea_missed_quote_window_and_closed_horizon(work):
    s,r,m,ids,assets=work;service=Ideas(s)
    a=service.save(idea(assets[1],status="watching",instrument_id=ids[1]));m[0]+=900001
    s.db.set_quote(ids[1],100,m[0]);service.collect(a["id"])
    assert service.outcomes(a["id"])["items"][0]["quality"]=="unavailable"
    m[0]=NOW+HOUR;s.db.set_quote(ids[1],100,m[0],event_at=m[0],received_at=m[0])
    b=service.save(idea(assets[1],status="watching",instrument_id=ids[1]));m[0]=NOW+HOUR+DAY
    s.db.insert_candles([candle(ids[1],m[0]-HOUR,110,"H1",HOUR)]);service.collect(b["id"])
    outcome=service.outcomes(b["id"])["items"][0]["outcomes"][0]
    assert outcome["return_pct"]==pytest.approx(10)
    assert outcome["actual_at"]==m[0]
    assert r.get("idea",b["id"])["status"]=="watching"


def test_basket_native_index_segments_and_incomplete_prices(work):
    s,r,m,ids,assets=work;service=Baskets(s)
    for i in ids:s.db.insert_candles([candle(i,NOW-HOUR,100,"H1",HOUR)])
    a=service.save({"title":"50 / 30 / Cash","members":[{"asset_uid":assets[0],"instrument_id":ids[0],"weight_pct":"50.00"},{"asset_uid":assets[1],"instrument_id":ids[1],"weight_pct":"30.00"}]})
    running=service.start(a["id"],{"expected_revision":a["revision_id"]});service.observe(a["id"])
    first=service.segments(a["id"])[0];assert first["status"]=="active"
    m[0]=NOW+HOUR
    for i,price in zip(ids,[120,90]):s.db.insert_candles([candle(i,NOW,price,"H1",HOUR)])
    service.observe(a["id"]);observation=service.observations(a["id"])["items"][-1]
    assert observation["index_value"]==pytest.approx(107)
    assert [a["contribution"] for a in observation["contributions"]]==pytest.approx([10,-3])
    m[0]+=HOUR;s.db.insert_candles([candle(ids[0],NOW+HOUR,130,"H1",HOUR)]);service.observe(a["id"])
    missing=service.observations(a["id"])["items"][-1];assert missing["index_value"] is None and missing["coverage_weight_pct"]==70
    changed=service.save({"members":[{"asset_uid":assets[0],"instrument_id":ids[0],"weight_pct":"80"}],"expected_revision":running["basket"]["revision_id"],"reason":"New composition starts a separate segment"},a["id"])
    assert any(x["status"]=="active" for x in service.segments(a["id"]))
    with pytest.raises(ResearchError):service.save({"title":"Bad weights","members":[{"asset_uid":assets[0],"instrument_id":ids[0],"weight_pct":"100.01"}]})


def test_playbook_snapshots_partitions_and_frozen_labels(work):
    s,r,m,ids,assets=work;book=Playbook(s);section=book.section({"title":"Confirmed launches","purpose":"Launches supported by original announcements"})
    examples=[]
    for k in range(12):
        m[0]=NOW+k*1000;ref=source(s,assets[1],k,m[0]);examples.append(book.example({"section_id":section["id"],"source_ref":ref,"label":"positive" if k%2==0 else "negative","explanation":"Original announcement must contain a verified mechanism.","information_direction":"negative"}))
    dataset=book.dataset(section["id"])
    assert set(dataset["partitions"]["train"]).isdisjoint(dataset["partitions"]["holdout"])
    e=examples[0];book.example({"label":"negative","expected_revision":e["revision_id"],"reason":"Changed my interpretation after checking source"},e["id"])
    assert r.dataset(dataset["id"])["examples"][0]["label"]=="positive"
    rule=book.rule({"section_id":section["id"],"text":"Original launches with independently verifiable product usage.","dataset_id":dataset["id"],"evidence_example_revision_ids":dataset["partitions"]["train"],"retrieval_threshold":0})
    evaluation=book.evaluate(rule["id"],dataset["id"])
    assert not evaluation["sufficient_sample"] and evaluation["precision"] is not None
    with pytest.raises(ResearchError):book.transition(rule["id"],{"status":"active","evaluation_id":evaluation["id"],"expected_revision":rule["revision_id"],"reason":"Try to promote an insufficient evaluation"})
    bad=book.rule({"section_id":section["id"],"text":"A leaked rule using future validation labels.","evidence_example_revision_ids":dataset["partitions"]["holdout"]})
    with pytest.raises(ResearchError,match="проверки использован"):book.evaluate(bad["id"],dataset["id"])


def test_playbook_unknown_require_abstains_and_backfill_is_idempotent(work):
    s,r,m,ids,assets=work;book=Playbook(s);section=book.section({"title":"Public launch patterns"})
    refs=[]
    for k in range(8):
        m[0]=NOW+k*1000;ref=source(s,assets[1],k,m[0]);refs.append(ref);book.example({"section_id":section["id"],"source_ref":ref,"label":"positive","explanation":"The source directly announces a verifiable public launch."})
    data=book.dataset(section["id"])
    rule=book.rule({"section_id":section["id"],"text":"Public launches and product adoption announcements.","dataset_id":data["id"],"evidence_example_revision_ids":data["partitions"]["train"],"retrieval_threshold":0,
        "code_conditions":[{"kind":"asset_in_watchlist","mode":"require","origin":"manual","value":True}]})
    decision=book.decide(rule,book.source_snapshot(refs[-1]),data["examples"],m[0]);assert decision["decision"]=="abstain"
    active=book.transition(rule["id"],{"status":"shadow","expected_revision":rule["revision_id"],"reason":"Start a manual shadow test"})
    book.backfill(active["id"],NOW-DAY,NOW+DAY);count=s.one("SELECT COUNT(*) AS n FROM research_object WHERE kind='match'")["n"]
    book.backfill(active["id"],NOW-DAY,NOW+DAY);assert s.one("SELECT COUNT(*) AS n FROM research_object WHERE kind='match'")["n"]==count


def test_model_budget_and_error_are_not_negative_predictions(work):
    s,r,m,ids,assets=work
    cfg={"provider":"xai","key":"test-secret","cheap":"test-model","strong":"test-model","daily_usd":"0","reserve_usd":"0.1"}
    client=httpx.Client(transport=httpx.MockTransport(lambda q:httpx.Response(500)))
    model=Model(s,cfg,client)
    with pytest.raises(ResearchError) as e:model.call("match",{})
    assert e.value.code=="budget_blocked"
    cfg["daily_usd"]="5"
    with pytest.raises(ResearchError):model.call("match",{})
    assert s.one("SELECT status FROM research_model_usage")["status"]=="error"
    assert 'test-secret' not in encode(model.status())


def test_lab_unsupported_factors_and_cost_policy(work):
    s,r,m,ids,assets=work;lab=Lab(s)
    spec={"title":"Launch reaction test","question":"How did price behave after a launch?","from_at":NOW-30*DAY,"to_at":NOW,"instrument_ids":ids}
    with pytest.raises(ResearchError) as e:lab.experiment({**spec,"factors":["narrative_momentum"]})
    assert e.value.code=="unsupported_factor"
    config=strategy_config();config["costs"]={"fee_bps":0,"slippage_bps":0}
    with pytest.raises(ResearchError):lab.strategy({"title":"Costless test","config":config})
    strategy=lab.strategy({"title":"Explicit idealized costs","config":config,"idealized_costs":True})
    assert strategy["idealized_costs"]


def test_descriptive_next_h1_open_overlap_and_missing():
    start=100*DAY;rows=[bar(start+i*HOUR,100+i,HOUR) for i in range(74)]
    episodes=[{"id":str(k),"instrument_id":1,"canonical_catalyst_id":str(k),"stage":"live","decision_at":start+HOUR+k*1000,"cohort":"all"} for k in range(2)]
    data={"h1":{"1":rows,"2":rows},"episodes":episodes,"cutoff_at":start+74*HOUR}
    spec={"horizons_days":[1],"benchmark_instrument_id":2}
    result=descriptive(data,spec)
    assert result["episodes"][0]["baseline_at"]==start+2*HOUR
    assert result["episodes"][1]["exclusion_reason"]=="overlap_cooldown"
    assert result["outcomes"][0]["return_pct"]==pytest.approx((125/102-1)*100)
    assert result["outcomes"][0]["excess_return_pct"]==0
    assert result["groups"][0]["quality"]=="insufficient_sample"


def simulation_data():
    start=200*DAY;daily=[bar(start-(4-k)*DAY,100 if k<2 else 105) for k in range(6)]
    daily[4].update({"close":120,"high":121,"open":110,"low":109})
    h1=[bar(start+i*HOUR,120,HOUR) for i in range(72)]
    data={"h1":{"1":deepcopy(h1),"2":deepcopy(h1)},"d1":{"1":deepcopy(daily),"2":deepcopy(daily)},"cutoff_at":start+72*HOUR,"data_mode":"reconstructed_current_rules","limitations":[]}
    spec={"experiment_type":"simulation","instrument_ids":[1],"benchmark_instrument_id":2,"from_at":start,"to_at":start+72*HOUR}
    strategy={"config":strategy_config(),"initial_capital":10000,"end_policy":"period_end","max_mark_age_hours":2,"order_type":"market"}
    return data,spec,strategy


def test_simulation_next_open_gap_stop_and_commissions():
    data,spec,strategy=simulation_data();result=simulate(data,spec,strategy)
    buys=[o for o in result["orders"] if o["kind"]=="buy"]
    assert buys
    assert all(o["at"]>=o["signal_time"] for o in buys)
    assert all(p["cash"]>=0 for p in result["equity"])
    assert result["metrics"]["fees_usdt"]>0
    entry_at=buys[0]["at"];target=next(b for b in data["h1"]["1"] if b["open_time"]==entry_at+HOUR);target.update({"open":90,"high":95,"low":80,"close":85})
    stopped=simulate(data,spec,strategy)
    assert stopped["trades"][0]["exit_reason"]=="gap_stop"
    assert stopped["trades"][0]["exit_price"]<90


def test_lab_frozen_inputs_subprocess_export_and_repeat(work):
    s,r,m,ids,assets=work;lab=Lab(s)
    for i in ids:
        s.db.insert_candles([candle(i,NOW-(95-k)*DAY,100+k) for k in range(95)])
        s.db.insert_candles([candle(i,NOW-(72-k)*HOUR,100+k,"H1",HOUR) for k in range(72)])
    e=lab.experiment({"title":"Frozen simulation inputs","question":"Does the fixed breakout strategy behave consistently?","experiment_type":"simulation","from_at":NOW-3*DAY,"to_at":NOW,"instrument_ids":ids})
    strategy=lab.strategy({"title":"Breakout test rules","config":strategy_config()});data=lab.prepare(e["id"])
    assert data["data_mode"]=="mixed_reconstruction"
    run=lab.create_run({"dataset_id":data["id"],"strategy_id":strategy["id"]});done=lab.run(run["id"])
    assert done["status"] in {"succeeded","incomplete"},done
    again=lab.create_run({"dataset_id":data["id"],"strategy_id":strategy["id"]});repeated=lab.run(again["id"])
    assert repeated["result"]["result_hash"]==done["result"]["result_hash"]
    archive=ZipFile(io.BytesIO(lab.export(run["id"])));manifest=json.loads(archive.read("manifest.json"))
    assert manifest["dataset"]["content_hash"]==data["content_hash"]
    cancelled=lab.create_run({"dataset_id":data["id"],"strategy_id":strategy["id"]});lab.cancel(cancelled["id"])
    assert lab.run(cancelled["id"])["status"]=="cancelled"


def test_workbench_api_auth_flags_and_background_results(work):
    s,r,m,ids,assets=work;cfg=Settings(auth_token="test",research_enabled=True,research_ideas_enabled=True,research_strength_enabled=True,
        research_baskets_enabled=True,research_playbook_enabled=True,research_lab_enabled=True)
    client=TestClient(create_app(s.db,cfg));headers={"Authorization":"Bearer test"}
    assert client.get('/api/research/v1/ideas').status_code==401
    assert client.get('/api/research/v1/workbench/config',headers=headers).json()['data']['features']['lab']
    diagnostic=client.get('/api/research/v1/playbook/status',headers=headers)
    assert diagnostic.status_code==200 and isinstance(diagnostic.json()['data']['queue'],list)
    create=client.post('/api/research/v1/ideas',headers={**headers,'Idempotency-Key':'idea1'},json=idea(assets[1]))
    assert create.status_code==200,create.text
    repeat=client.post('/api/research/v1/ideas',headers={**headers,'Idempotency-Key':'idea1'},json=idea(assets[1]))
    assert repeat.json()['data']['id']==create.json()['data']['id']
    bad=client.post('/api/research/v1/ideas',headers={**headers,'Origin':'https://evil.test'},json=idea(assets[1]));assert bad.status_code==403
    off=TestClient(create_app(s.db,Settings(auth_token='test',research_enabled=True)))
    assert off.get('/api/research/v1/ideas',headers=headers).status_code==503
    runner=ResearchRunner(s.db,cfg);runner.s=s;job=runner.enqueue('strength',{'instrument_ids':ids});runner.step()
    assert runner.job(job['id'])['status']=='succeeded'
    assert runner.job(job['id'])['result']['snapshot_id']


def test_idea_external_import_is_draft_local_now_and_preserves_owner_edits(work):
    s,r,m,ids,assets=work;service=Ideas(s)
    body={"external_id":"external-1","idea":{"ticker":"ETH","title":"An archived investment thesis","thesis":"An externally written but testable thesis.","marketSource":"coingecko","marketSourceId":"ethereum","createdAt":NOW-DAY,"signalAt":NOW-DAY,"signalPrice":50}}
    first=service.import_external(body);assert first["status"]=="draft" and first["available_at"]==NOW
    assert first["external_origin"]["created_at"]<first["available_at"]
    assert service.import_external(body)["revision_id"]==first["revision_id"]
    changed=service.save({"thesis":"My own interpretation must survive the next external refresh.","expected_revision":first["revision_id"],"reason":"Reviewed the original source personally"},first["id"])
    update=deepcopy(body);update["idea"]["thesis"]="A subsequently changed external thesis."
    assert service.import_external(update)["conflict"] is True
    assert r.get("idea",first["id"])["thesis"]==changed["thesis"]


def test_basket_same_bar_observation_is_not_duplicated(work):
    s,r,m,ids,assets=work;service=Baskets(s);s.db.insert_candles([candle(ids[0],NOW-HOUR,100,"H1",HOUR)])
    b=service.save({"title":"Idempotent observations","members":[{"asset_uid":assets[0],"instrument_id":ids[0],"weight_pct":"50"}]})
    service.start(b["id"],{"expected_revision":b["revision_id"]});service.observe(b["id"])
    first=service.observations(b["id"])["items"];m[0]+=1000;service.observe(b["id"])
    assert len(first)==len(service.observations(b["id"])["items"])==1


def test_simulation_future_bars_do_not_change_prior_orders_and_stop_wins():
    data,spec,strategy=simulation_data();baseline=simulate(data,spec,strategy)
    future=deepcopy(data);future["h1"]["1"].append(bar(spec["to_at"]+DAY,999999,HOUR));future["d1"]["1"].append(bar(spec["to_at"]+DAY,999999))
    assert simulate(future,spec,strategy)["orders"]==baseline["orders"]
    entry=next(o for o in baseline["orders"] if o["kind"]=="buy")
    row=next(b for b in data["h1"]["1"] if b["open_time"]==entry["at"]);row.update({"high":200,"low":80})
    stopped=simulate(data,spec,strategy);assert stopped["trades"][0]["exit_reason"]=="stop"


def test_model_xai_alias_is_private_and_usage_is_recorded(work,monkeypatch,tmp_path):
    from app.research.model import terminal_settings
    s,r,m,ids,assets=work
    monkeypatch.setenv("XAI","private-fixture-key");monkeypatch.delenv("LLM_PROVIDER",raising=False)
    settings=terminal_settings(tmp_path);assert settings["provider"]=="xai" and settings["key"]=="private-fixture-key"
    settings.update({"daily_usd":"5","reserve_usd":"0.1"})
    client=httpx.Client(transport=httpx.MockTransport(lambda q:httpx.Response(200,json={"id":"request-1","choices":[{"message":{"content":'{"matches":[]}'}}],"usage":{"prompt_tokens":10,"completion_tokens":3}})))
    result=Model(s,settings,client).call("match",{"patterns":[]})
    assert result["output"]=={"matches":[]}
    assert s.one("SELECT status FROM research_model_usage")["status"]=="succeeded"
    assert 'private-fixture-key' not in encode(result)


def test_shared_story_never_crosses_playbook_partitions(work):
    s,r,m,ids,assets=work;book=Playbook(s);section=book.section({"title":"Story grouped examples"});first_ref=None
    for k in range(10):
        m[0]=NOW+k*1000;ref=source(s,assets[1],k,m[0]);first_ref=first_ref or ref
        book.example({"section_id":section["id"],"source_ref":first_ref if k==9 else ref,"label":"positive","explanation":"The original story must remain within one partition."})
    data=book.dataset(section["id"])
    for revisions in data["story_groups"].values():
        assert sum(bool(set(revisions)&set(part)) for part in data["partitions"].values())==1


def test_same_terminal_revision_from_another_archive_does_not_duplicate(work):
    from app.research.integration import Integration
    from app.research.contracts import VERSION
    s,r,m,ids,assets=work;integration=Integration(s,Settings(research_enabled=True))
    payload={"display_symbol":"NEW","name":"An imported asset"};item={"entity_kind":"assets","entity_id":"new","revision_id":"v1","operation":"upsert","change_seq":1,"payload":payload,"payload_hash":digest(payload)}
    for dataset in ("archive:first","archive:second"):
        count,bad,_=integration.page({"schema_version":VERSION,"dataset_id":dataset,"items":[item],"next_cursor":"end","has_more":False},state={"snapshot_watermark":"complete"},snapshot=True)
        assert bad==0
    assert s.one("SELECT COUNT(*) AS n FROM research_asset WHERE display_symbol='NEW'")["n"]==1


def test_ai_idea_draft_requires_quotes_and_never_activates(work):
    s,r,m,ids,assets=work;ref=source(s,assets[1],55);service=Ideas(s)
    class FakeModel:
        def call(self,*args,**kwargs):return {"output":{"title":"A sourced hypothesis draft","thesis":"The public launch might increase demand for network services.","mechanism":"Usage could create additional demand for network services.",
            "confirmation":["Verify independently reported network usage"],"invalidation":["The team cancels the announced public launch"],"review_condition":"After the next original announcement",
            "claims":[{"text":"A public launch was announced","quote":"The team confirms a public network launch","source_revision_id":ref["revision_id"]}]},"usage":{"model":"fixture","actual_usd":"0"}}
    value=service.ai_draft({"source_refs":[ref],"primary_asset_uid":assets[1]},FakeModel())
    assert value["status"]=="draft" and value["provenance"]=="ai_draft"
    class BadModel(FakeModel):
        def call(self,*a,**k):
            result=super().call(*a,**k);result["output"]["claims"][0]["quote"]="An invented announcement absent from the original";return result
    with pytest.raises(ResearchError) as error:service.ai_draft({"source_refs":[ref]},BadModel())
    assert error.value.code=="unverified_claim"


def test_imported_basket_history_remains_external_until_local_start(work):
    s,r,m,ids,assets=work;service=Baskets(s)
    state={"assets":[{"id":"one","ticker":"BTC","name":"Bitcoin","provider":"coingecko","assetId":"bitcoin","weight":50,"base":100,"price":120,"target":None,"at":NOW-DAY,"sector":""}],
        "startedAt":NOW-2*DAY,"history":[{"at":NOW-DAY,"index":110}],"archived":[],"refreshedAt":NOW-DAY,"warnings":[]}
    value=service.import_external({"external_id":"basket-1","state":state,"title":"An archived original basket"})
    assert value["status"]=="draft" and not service.segments(value["id"])
    assert value["external_origin"]["index_at_archive"]["value"]==110
    assert service.import_external({"external_id":"basket-1","state":state,"title":"An archived original basket"})["revision_id"]==value["revision_id"]


def test_lab_parameter_selection_excludes_holdout(work,monkeypatch):
    s,r,m,ids,assets=work;lab=Lab(s);cfg=strategy_config();cfg["risk"]["time_stop"]=24
    e=lab.experiment({"title":"Chronological selection test","question":"Does parameter selection avoid reading the final holdout?","experiment_type":"simulation","from_at":NOW-120*DAY,"to_at":NOW,"instrument_ids":ids})
    strategy=lab.strategy({"title":"A fixed sweep strategy","config":cfg});data=lab.prepare(e["id"])
    sweep=lab.create_sweep({"dataset_id":data["id"],"strategy_id":strategy["id"],"train_to":NOW-80*DAY,"validation_to":NOW-40*DAY,
        "variants":[{"risk.stop_loss_pct":5},{"risk.stop_loss_pct":10}],"embargo_hours":24})
    observed=[]
    def fake_run(identifier,should_stop=lambda:False):
        run=lab.result(identifier);frozen=r.dataset(run["dataset_id"]);part=frozen["partition"]["name"];risk=run["strategy"]["config"]["risk"]["stop_loss_pct"]
        observed.append((part,risk));score=(20 if risk==5 else 10) if part=="validation" else (-100 if risk==5 else 1000) if part=="holdout" else 0
        return {**run,"status":"succeeded","result":{"metrics":{"return_pct":score}}}
    monkeypatch.setattr(lab,"run",fake_run)
    for _ in range(3):lab.execute_sweep(sweep["id"])
    report=lab.sweep(sweep["id"])
    assert report["selected_variant"]==0
    assert [x for x in observed if x[0]=="holdout"]==[("holdout",5)]
    assert report["holdout_result"]["metrics"]["return_pct"]==-100


def test_simulation_costs_match_independent_decimal_reference():
    data,spec,strategy=simulation_data();result=simulate(data,spec,strategy)
    assert result["trades"][0]["net_pnl"]==pytest.approx(-14.977526221904033,abs=1e-8)


def test_archive_import_api_keeps_historical_availability_honest(work):
    from app.research.contracts import VERSION
    s,r,m,ids,assets=work;cfg=Settings(auth_token='test',research_enabled=True,research_ideas_enabled=True)
    client=TestClient(create_app(s.db,cfg));headers={'Authorization':'Bearer test'}
    p={"display_symbol":"NEW","name":"Archive new asset"};payload={"schema_version":VERSION,"dataset_id":"archive:"+'a'*64,"archive_captured_at":NOW-DAY,
        "items":[{"entity_kind":"assets","entity_id":"new","revision_id":"1","operation":"upsert","change_seq":1,"payload":p,"payload_hash":digest(p)}],"next_cursor":"end","has_more":False}
    result=client.post('/api/research/v1/integration/import-batch',headers=headers,json=payload)
    assert result.status_code==200,result.text
    assert s.state('terminal')['last_success_at']==NOW-DAY
    assert s.one("SELECT created_at FROM research_asset WHERE display_symbol='NEW'")['created_at']>NOW-DAY
    invalid=client.post('/api/research/v1/ideas',headers=headers,json={'title':False})
    assert invalid.status_code==422


def test_playbook_explicit_retry_preserves_failures_and_successes(work,monkeypatch):
    s,r,m,ids,assets=work;book=Playbook(s);section=book.section({"title":"Retry only failed evaluations"})
    for k in range(12):
        m[0]=NOW+k*1000;book.example({"section_id":section["id"],"source_ref":source(s,assets[1],k,m[0]),"label":"positive","explanation":"A verifiable original product announcement."})
    data=book.dataset(section["id"]);rule=book.rule({"section_id":section["id"],"text":"Verified original public product announcements.","dataset_id":data["id"],"evidence_example_revision_ids":data["partitions"]["train"]})
    calls=[]
    def decide(*args):
        calls.append(1)
        if len(calls)==1:raise ResearchError("model_error","Temporary provider failure")
        return {"decision":"match"}
    monkeypatch.setattr(book,"decide",decide)
    first=book.evaluate(rule["id"],data["id"]);assert first["counts"]["failed"]==1
    count=len(calls);again=book.evaluate(rule["id"],data["id"]);assert len(calls)==count and again["counts"]["failed"]==1
    fixed=book.evaluate(rule["id"],data["id"],retry_key="explicit-retry");assert fixed["status"]=="completed" and len(calls)==count+1
    assert r.dataset(first["id"])["status"]=="incomplete"
    book.evaluate(rule["id"],data["id"],retry_key="explicit-retry");assert len(calls)==count+1
    other=book.section({"title":"Different research topic"})
    with pytest.raises(ResearchError,match="Другая рубрика"):book.rule({"section_id":other["id"],"text":"Rule must not borrow another section data.","dataset_id":data["id"]})


def test_distillation_sends_bounded_train_evidence_with_exact_lineage(work):
    s,r,m,ids,assets=work;captured=[]
    class Provider:
        def call(self,operation,payload,strong=False):
            captured.append(payload)
            assert operation=="distill" and strong and len(encode(payload))<=45000
            assert 'raw_snapshot' not in encode(payload)
            assert {e["label"] for e in payload["examples"]}=={"positive","negative"}
            return {"output":{"summary_ru":"Оригинальные подтверждённые запуски с проверяемым механизмом.","signs_ru":["Есть первичный анонс","Есть описание применения"],"anti_signs_ru":["Нет источника"],"direction":"watch","horizon":"1-2w","check_ru":[],"flags":{"author":False,"known_ticker":False,"image":False}},"usage":{"model":"controlled-fixture"}}
    book=Playbook(s,Provider());section=book.section({"title":"Bounded original examples"})
    for k in range(60):
        m[0]=NOW+k*1000;book.example({"section_id":section["id"],"source_ref":source(s,assets[1],k,m[0]),"label":"positive" if k%2==0 else "negative","explanation":"Check the original announcement and product usage. "*60})
    data=book.dataset(section["id"]);rule=book.distill(section["id"],data["id"])
    assert set(rule["evidence_example_revision_ids"])=={e["revision_id"] for e in captured[0]["examples"]}
    assert set(rule["evidence_example_revision_ids"])<=set(data["partitions"]["train"])
    assert rule["status"]=="draft" and rule["provenance"]=="model"


def test_match_snapshot_excludes_later_analysis(work):
    s,r,m,ids,assets=work;ref=source(s,assets[1],999)
    future=NOW+HOUR
    s.execute("INSERT INTO research_analysis VALUES(?,?,?,?,?)",("future-analysis",ref["revision_id"],future,1,encode({"available_at":future,"claims":[{"verified":True}],"summary":"Later analysis must not change the saved matching evidence"})))
    m[0]=NOW+2*HOUR;book=Playbook(s)
    assert book.source_snapshot(ref,NOW)["verified_quote"] is None
    assert book.source_snapshot(ref)["verified_quote"] is True
    material=s.material(ref["id"],NOW,ref["revision_id"])
    assert material["revision_id"]==ref["revision_id"] and material["analyses"]==[]
    with pytest.raises(ResearchError):s.material(ref["id"],NOW,"unknown-version")
