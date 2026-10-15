"""Research 03–06 routes; shares authentication, Origin checks and idempotency."""
from fastapi import Body,Header,Query
from fastapi.responses import Response
from ..research.contracts import ResearchError,decode,digest
from ..research.records import Records
from ..research.ideas import Ideas
from ..research.baskets import Baskets
from ..research.strength import Strength
from ..research.playbook import Playbook
from ..research.lab import Lab
from ..research.model import Model


def register(router,runner,settings,result,mutate):
    s=runner.s;r=Records(s);ideas=Ideas(s);baskets=Baskets(s);strength=Strength(s);playbook=Playbook(s);lab=Lab(s)
    def check(flag):
        if not settings.research_enabled or not getattr(settings,"research_"+flag+"_enabled"):
            raise ResearchError("disabled","Этот исследовательский сервис выключен",503)
    def job(kind,body,flag,key=None):
        check(flag);value=runner.enqueue(kind,body,key=(key or "manual")+":"+digest(body))
        return result({"job_id":value["id"],"status":value["status"]})
    def lists(path,kind,flag):
        def listing(status:str|None=None,asset_uid:str|None=None,q:str="",due_before:int|None=None,cursor:str|None=None,limit:int=Query(50,ge=1,le=100)):
            check(flag);return result(r.list(kind,status=status,asset_uid=asset_uid,text_query=q,due_before=due_before,after=cursor,limit=limit))
        def get(identifier:str,revision_id:str|None=None,as_of:int|None=None):
            check(flag);return result(r.get(kind,identifier,revision_id,as_of))
        def history(identifier:str,cursor:str|None=None,limit:int=Query(50,ge=1,le=100)):
            check(flag);return result(r.history(kind,identifier,cursor,limit))
        router.add_api_route(path,listing,methods=["GET"],name=kind+"_list")
        router.add_api_route(path+"/{identifier}",get,methods=["GET"],name=kind+"_get")
        router.add_api_route(path+"/{identifier}/history",history,methods=["GET"],name=kind+"_history")
    def writes(path,flag,save):
        def create(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
            check(flag);return mutate(path+":create",key,body,lambda:save(body))
        def patch(identifier:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
            check(flag);return mutate(path+":"+identifier,key,body,lambda:save(body,identifier))
        router.add_api_route(path,create,methods=["POST"],name=path+"_create")
        router.add_api_route(path+"/{identifier}",patch,methods=["PATCH"],name=path+"_patch")
    def action(path,flag,fn):
        def apply(identifier:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
            check(flag);return mutate(path+":"+identifier,key,body,lambda:fn(identifier,body))
        router.add_api_route(path,apply,methods=["POST"],name=path)

    @router.get("/workbench/config")
    def config():
        return result({"enabled":settings.research_enabled,"features":{key:getattr(settings,"research_"+key+"_enabled") for key in
            ("ideas","strength","baskets","playbook","playbook_matching","playbook_memory","lab")},"model":Model(s).status(),
            "limits":{"strength_instruments":200,"correlation_instruments":30,"basket_members":30,"lab_instruments":20,"lab_days":366,"lab_timeout_seconds":60,"lab_memory_mb":512}})

    @router.post("/ideas/import")
    def import_idea(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        check("ideas");return mutate("external_idea_import",key,body,lambda:ideas.import_external(body))
    @router.post("/ideas/prepare-draft",status_code=202)
    def ai_draft(body:dict=Body(...)):return job("idea_draft",body,"ideas")
    lists("/ideas","idea","ideas");writes("/ideas","ideas",ideas.save)
    @router.get("/instruments/{instrument_id}/research-links")
    def technical_links(instrument_id:int):
        check("ideas")
        return result({"htf_ideas":s.rows("SELECT id,scenario_id,rule_version FROM htf_idea WHERE instrument_id=? ORDER BY id DESC LIMIT 10",(instrument_id,)),
            "ltf_scenarios":s.rows("SELECT s.id,s.state FROM ltf_scenario s JOIN ltf_observation o ON o.id=s.observation_id WHERE o.instrument_id=? ORDER BY s.id DESC LIMIT 10",(instrument_id,))})
    action("/ideas/{identifier}/transition","ideas",ideas.transition)
    action("/ideas/{identifier}/reviews","ideas",ideas.review)
    action("/ideas/{identifier}/links","ideas",ideas.add_link)
    action("/ideas/{identifier}/copy","ideas",ideas.copy)
    @router.get("/ideas/{identifier}/outcomes")
    def outcomes(identifier:str):
        check("ideas");return result(ideas.outcomes(identifier))
    @router.get("/ideas/{identifier}/reviews")
    def reviews(identifier:str,cursor:str|None=None):
        check("ideas");r.get("idea",identifier);return result(r.notes(identifier,None,cursor))
    @router.post("/ideas/{identifier}/refresh",status_code=202)
    def refresh_idea(identifier:str):return job("idea_refresh",{"id":identifier},"ideas",identifier)

    @router.get("/strength")
    def list_strength(instrument_ids:str="",asset_uids:str="",metric:str="excess_30",sort:str="desc",cursor:str|None=None,snapshot_id:str|None=None,
        limit:int=Query(100,ge=1,le=200),window:int|None=None,excess_min:float|None=None,excess_max:float|None=None,correlation_min:float|None=None,
        correlation_max:float|None=None,volatility_min:float|None=None,volatility_max:float|None=None,quality:str|None=None):
        check("strength")
        def parse_ids(raw):
            try:return [int(x) if not x.startswith("alt:") else x for x in raw.split(",") if x]
            except ValueError:raise ResearchError("invalid_instrument","Некорректный ID инструмента",400)
        if (excess_min is not None or excess_max is not None) and window not in {7,30,90}:raise ResearchError("window_required","Для фильтра К BTC укажите окно 7/30/90",400)
        filters={k:v for k,v in {"excess_"+str(window)+"_min":excess_min,"excess_"+str(window)+"_max":excess_max,"correlation_60_min":correlation_min,
            "correlation_60_max":correlation_max,"volatility_20_min":volatility_min,"volatility_20_max":volatility_max,"quality":quality}.items() if v is not None}
        return result(strength.list(snapshot_id=snapshot_id,instrument_ids=parse_ids(instrument_ids),asset_uids=[x for x in asset_uids.split(",") if x],metric=metric,sort=sort,after=cursor,limit=limit,filters=filters))
    @router.get("/assets/{asset_uid}/strength")
    def asset_strength(asset_uid:str):
        check("strength");s.require_asset(asset_uid);return result(strength.list(asset_uids=[asset_uid]))
    @router.post("/strength/recalculate",status_code=202)
    def recalculate(body:dict=Body(default={})):return job("strength",body,"strength")
    @router.post("/correlations",status_code=202)
    def calculate_correlations(body:dict=Body(...)):
        ids=body.get("instrument_ids",[])
        if not isinstance(ids,list) or not 1<=len(ids)<=30:raise ResearchError("scope_limit","От 1 до 30 инструментов")
        return job("correlations",body,"strength")
    @router.get("/correlations/{identifier}")
    def correlations(identifier:str):check("strength");return result(r.dataset(identifier,"correlations"))

    lists("/baskets","basket","baskets");writes("/baskets","baskets",baskets.save)
    @router.post("/baskets/import")
    def import_basket(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        check("baskets");return mutate("external_basket_import",key,body,lambda:baskets.import_external(body))
    action("/baskets/{identifier}/start","baskets",baskets.start)
    action("/baskets/{identifier}/transition","baskets",baskets.transition)
    @router.get("/baskets/{identifier}/segments")
    def segments(identifier:str):check("baskets");return result({"items":baskets.segments(identifier)})
    @router.get("/baskets/{identifier}/observations")
    def observations(identifier:str,segment_id:str|None=None,from_at:int|None=Query(None,alias="from"),to_at:int|None=Query(None,alias="to"),cursor:str|None=None,limit:int=Query(100,ge=1,le=100)):
        check("baskets");return result(baskets.observations(identifier,segment_id,from_at,to_at,cursor,limit))
    @router.post("/baskets/{identifier}/observe",status_code=202)
    def observe(identifier:str):return job("basket_observe",{"id":identifier},"baskets",identifier)

    lists("/playbook/sections","section","playbook");writes("/playbook/sections","playbook",playbook.section)
    lists("/playbook/examples","example","playbook");writes("/playbook/examples","playbook",playbook.example)
    lists("/playbook/rules","rule","playbook");writes("/playbook/rules","playbook",playbook.rule)
    action("/playbook/rules/{identifier}/transition","playbook",playbook.transition)
    lists("/playbook/matches","match","playbook")
    action("/playbook/matches/{identifier}/feedback","playbook",playbook.feedback)
    @router.post("/playbook/matches/{identifier}/retry",status_code=202)
    def retry_match(identifier:str):return job("playbook_retry",{"id":identifier},"playbook",identifier)
    @router.post("/playbook/datasets")
    def playbook_dataset(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        check("playbook");return mutate("playbook_dataset",key,body,lambda:playbook.dataset(body["section_id"]))
    @router.get("/playbook/datasets/{identifier}")
    def get_playbook_dataset(identifier:str):check("playbook");return result(r.dataset(identifier,"playbook"))
    @router.get("/playbook/sections/{identifier}/dataset")
    def section_dataset(identifier:str):
        check("playbook");reference=s.state("playbook-dataset:"+identifier).get("dataset_id")
        return result(r.dataset(reference,"playbook") if reference else None)
    @router.post("/playbook/sections/{identifier}/distill",status_code=202)
    def distill(identifier:str,body:dict=Body(...)):return job("playbook_distill",{**body,"id":identifier},"playbook",identifier)
    @router.post("/playbook/rules/{identifier}/evaluate",status_code=202)
    def evaluate(identifier:str,body:dict=Body(...)):return job("playbook_evaluate",{**body,"id":identifier},"playbook",identifier)
    @router.post("/playbook/rules/{identifier}/backfill",status_code=202)
    def backfill(identifier:str,body:dict=Body(...)):return job("playbook_backfill",{**body,"id":identifier},"playbook",identifier)
    @router.get("/playbook/evaluations/{identifier}")
    def evaluation(identifier:str):check("playbook");return result(r.dataset(identifier,"playbook_evaluation"))
    @router.get("/playbook/status")
    def playbook_status():check("playbook");return result({"model":Model(s).status(),"retrieval":"native lexical TF-IDF; model comparison is an explicit rule setting",
        "matching_enabled":settings.research_playbook_matching_enabled,"memory_enabled":settings.research_playbook_memory_enabled,
        "queue":s.rows("SELECT kind,status,COUNT(*) AS n FROM research_job WHERE kind LIKE ? GROUP BY kind,status",("playbook_%",))})
    @router.post("/playbook/model-check",status_code=202)
    def model_check():return job("model_probe",{},"playbook","probe")

    lists("/lab/experiments","experiment","lab");writes("/lab/experiments","lab",lab.experiment)
    lists("/lab/strategies","strategy","lab");writes("/lab/strategies","lab",lab.strategy)
    action("/lab/strategies/{identifier}/versions","lab",lambda identifier,body:lab.strategy(body,identifier))
    @router.post("/lab/datasets/prepare",status_code=202)
    def prepare(body:dict=Body(...)):return job("lab_prepare",body,"lab",body.get("experiment_id","manual"))
    @router.get("/lab/datasets/{identifier}")
    def dataset(identifier:str,include_inputs:bool=False):
        check("lab");data=r.dataset(identifier,"lab")
        return result(data if include_inputs else {k:v for k,v in data.items() if k not in {"h1","d1","catalyst_revisions","episodes"}})
    @router.get("/lab/experiments/{identifier}/datasets")
    def experiment_datasets(identifier:str):
        check("lab");r.get("experiment",identifier)
        rows=s.rows("SELECT id,created_at,content_hash FROM research_dataset WHERE kind='lab' AND json_extract(payload,'$.experiment_id')=? ORDER BY created_at DESC,id DESC LIMIT 20",(identifier,))
        return result({"items":rows})
    @router.get("/lab/runs")
    def runs(limit:int=Query(50,ge=1,le=100)):
        check("lab");rows=s.rows("SELECT id FROM research_result WHERE kind='lab_run' ORDER BY created_at DESC,id DESC LIMIT ?",(limit,))
        return result({"items":[lab.result(row["id"],False) for row in rows]})
    @router.post("/lab/runs",status_code=202)
    def create_run(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        check("lab")
        def create():
            run=lab.create_run(body);j=runner.enqueue("lab_run",{"id":run["id"]},key=run["id"]);return {**run,"job_id":j["id"]}
        return mutate("lab_run_create",key,body,create)
    @router.get("/lab/runs/{identifier}")
    def run(identifier:str):check("lab");return result(lab.result(identifier,False))
    @router.post("/lab/runs/{identifier}/cancel")
    def cancel(identifier:str):check("lab");return result(lab.cancel(identifier))
    @router.get("/lab/runs/{identifier}/episodes")
    def episodes(identifier:str,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=100)):
        check("lab");return result(lab.rows(identifier,"episodes",offset,limit))
    @router.get("/lab/runs/{identifier}/trades")
    def trades(identifier:str,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=100)):
        check("lab");return result(lab.rows(identifier,"trades",offset,limit))
    @router.get("/lab/runs/{identifier}/chart")
    def run_chart(identifier:str):
        check("lab");data=lab.result(identifier);points=data.get("result",{}).get("equity",[]);step=max(1,(len(points)+499)//500)
        selected=points[::step]
        # A sampled interval must not hide stale marks inside that interval.
        selected=[{**point,"quality":"stale" if any(p.get("quality")!="ok" for p in points[index*step:(index+1)*step]) else point.get("quality","ok")} for index,point in enumerate(selected)]
        if points and selected[-1]!=points[-1]:selected.append(points[-1])
        return result({"points":selected,"sampled":step>1,"total":len(points)})
    @router.get("/lab/runs/{identifier}/export")
    def export(identifier:str):
        check("lab");return Response(lab.export(identifier),media_type="application/zip",headers={"Content-Disposition":'attachment; filename="research-'+identifier+'.zip"'})
    @router.post("/lab/sweeps",status_code=202)
    def sweep_create(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        check("lab")
        def create():
            sweep=lab.create_sweep(body);j=runner.enqueue("lab_sweep",{"id":sweep["id"]},key=sweep["id"]);return {**sweep,"job_id":j["id"]}
        return mutate("lab_sweep",key,body,create)
    @router.get("/lab/sweeps/{identifier}")
    def sweep_get(identifier:str):check("lab");return result(lab.sweep(identifier))
