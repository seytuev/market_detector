"""Authenticated versioned API for information and external catalysts."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ..research.contracts import ResearchError, decode, uid,encode,timestamp,text
from ..research.catalysts import Catalysts
from ..research.jobs import ResearchRunner

log = logging.getLogger(__name__)


class ResearchRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                return await original(request)
            except ResearchError as error:
                return JSONResponse(status_code=error.status, content={"error":{
                    "code":error.code,"message":error.message,"field_errors":error.fields,"request_id":uid()}})
            except (KeyError,TypeError,ValueError,OverflowError) as error:
                log.info("Invalid research request %s (%s)",request.url.path,type(error).__name__)
                return JSONResponse(status_code=422,content={"error":{"code":"invalid_request","message":"Проверьте обязательные поля и типы значений.","field_errors":{},"request_id":uid()}})
        return handle


def register_research_routes(app, db, settings, require_auth):
    runner = ResearchRunner(db,settings)
    app.state.research = runner
    s, catalysts = runner.s, Catalysts(runner.s)
    router = APIRouter(prefix="/api/research/v1", dependencies=[Depends(require_auth)],route_class=ResearchRoute)

    def enabled():
        if not settings.research_enabled:
            raise ResearchError("disabled","Исследовательский модуль выключен",503)

    def result(value):
        return s.envelope(value,settings,quality="ok" if settings.research_enabled else "disabled")

    def mutate(operation, key, body, function):
        enabled()
        return result(s.idempotent(operation,key,body,function))

    @router.get('/credentials')
    def credential_status():
        from ..research.credentials import Credentials
        from ..research.model import terminal_settings
        import os
        credentials=Credentials(s)
        return result({'items':{name:credentials.status(name,terminal_settings(provider=name)['key'])
            for name in ('xai','openai','anthropic')} | {'x':credentials.status('x',os.environ.get('X_BEARER_TOKEN',''))}})

    @router.put('/credentials/{name}')
    def save_credential(name:str,body:dict=Body(...)):
        from ..research.credentials import Credentials
        enabled()
        # Do not pass secrets through the idempotency cache or general settings audit.
        return result(Credentials(s).save(name,body))

    @router.delete('/credentials/{name}')
    def remove_credential(name:str):
        from ..research.credentials import Credentials
        enabled();return result(Credentials(s).remove(name))

    @router.get("/config")
    def config():
        return result({"enabled":settings.research_enabled,"context_ui_enabled":settings.research_context_ui_enabled,
                       "notifications_enabled":settings.research_notifications_enabled,
                       "sources":{"terminal":runner.integration.status(),"deribit":settings.research_deribit_enabled}})

    @router.get("/assets")
    def assets(q: str="",cursor: str|None=None,limit:int=Query(50,ge=1,le=100)):
        return result(s.assets(q,cursor,limit))

    @router.get("/overview")
    def overview():
        return result(s.overview() if settings.research_enabled else {"items":[]})

    @router.post("/assets")
    def create_asset(body:dict=Body(...), key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("create_asset",key,body,lambda:s.create_asset(body))

    @router.get("/assets/{asset_uid}")
    def asset(asset_uid:str):
        return result(s.asset(asset_uid))

    @router.post("/assets/{asset_uid}/external-identifiers")
    def verify_external(asset_uid:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        def apply():
            reason=text(body.get("reason"),"reason",5,1000);value=s.asset(asset_uid)
            refs=body.get("external_ids",[])
            if not isinstance(refs,list) or not refs:raise ResearchError("identity_required","Нужен внешний идентификатор")
            combined={ (ref["namespace"],ref["external_id"]):ref for ref in [*value["external_ids"],*refs] }
            updated=s.create_asset({**value,"external_ids":list(combined.values())},asset_uid)
            s.audit("asset",asset_uid,{"action":"verify_external_identity","reason":reason,"external_ids":refs})
            return updated
        return mutate("verify_asset:"+asset_uid,key,body,apply)

    @router.post("/instruments/{instrument_id}/asset-link")
    def link(instrument_id:int,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("link:"+str(instrument_id),key,body,lambda:s.link(instrument_id,body))

    @router.get("/instruments/{instrument_id}/context")
    def context(instrument_id:int,as_of:int|None=None):
        with db.read_tx():
            return s.context(instrument_id,settings,as_of)

    @router.get("/assets/{asset_uid}/materials")
    def materials(asset_uid:str,cursor:str|None=None,limit:int=Query(50,ge=1,le=100),
                  from_at:int|None=Query(None,alias="from"),to_at:int|None=Query(None,alias="to"),kind:str|None=None):
        with db.read_tx():
            return result(s.materials(asset_uid,after=cursor,limit=limit,from_at=from_at,to_at=to_at,kind=kind))

    @router.get("/feed")
    def feed(before:int|None=None,before_id:str="",limit:int=Query(50,ge=1,le=100)):
        enabled()
        with db.read_tx():
            moment=s.clock()
            rows=s.rows("SELECT r.material_id AS id,r.revision_id,r.payload,r.available_at,"
                "COALESCE(r.published_at,r.available_at) AS published_at FROM research_material_revision r "
                "WHERE r.available_at<=? AND r.lifecycle='active' "
                "AND NOT EXISTS(SELECT 1 FROM research_material_revision n WHERE n.material_id=r.material_id "
                "AND n.available_at<=? AND (n.available_at>r.available_at OR "
                "(n.available_at=r.available_at AND (n.change_seq>r.change_seq OR "
                "(n.change_seq=r.change_seq AND n.revision_id>r.revision_id))))) "
                "AND (CAST(? AS BIGINT) IS NULL OR COALESCE(r.published_at,r.available_at)<? OR "
                "(COALESCE(r.published_at,r.available_at)=? AND r.material_id>?)) "
                "AND r.source_kind IN ('telegram','x','twitter') "
                "ORDER BY published_at DESC,r.material_id LIMIT ?",(moment,moment,before,before,before,before_id,limit+1))
            items=[{**decode(row['payload']),**{k:row[k] for k in ('id','revision_id','available_at','published_at')}} for row in rows[:limit]]
            from ..research.telegram import TelegramFeed
            TelegramFeed(s,settings).attach(items)
            return result({"items":items,"has_more":len(rows)>limit})

    @router.get("/telegram")
    def telegram_view():
        enabled()
        from ..research.telegram import TelegramFeed
        with db.read_tx():
            feed=TelegramFeed(s,settings)
            stories=feed.stories()
            latest=feed.latest_digest()
            if latest:
                _,_,signature,_=feed.digest_input()
                saved=s.one("SELECT input_hash FROM research_telegram_digest WHERE id=?",(latest["id"],))
                latest["superseded"]=saved["input_hash"]!=signature
            return result({**stories,"digest":latest,"model":feed.model.status(),
                "analysis_enabled":settings.research_telegram_analysis_enabled,"digest_enabled":settings.research_telegram_digest_enabled,
                "jobs":s.rows("SELECT id,kind,status,error_code,updated_at FROM research_job WHERE kind IN ('telegram_analysis','telegram_digest') ORDER BY CASE WHEN status='running' THEN 0 WHEN status='pending' THEN 1 ELSE 2 END,updated_at DESC,id DESC LIMIT 20")})

    @router.post("/telegram/process",status_code=202)
    def telegram_process():
        enabled()
        from ..research.telegram import TelegramFeed
        from ..research.channels import Channels
        feed=TelegramFeed(s,settings);feed.check()
        jobs=[]
        for channel in Channels(s).list():
            if channel["enabled"]:
                jobs.append(runner.enqueue("channel",{"handle":channel["handle"]},key=channel["handle"])["id"])
        for row in feed.rows(limit=50):
            job=feed.queue(runner,row["id"],manual=True)
            if job:jobs.append(job["id"])
        return result({"job_ids":jobs,"message":"Сбор и обработка поставлены в очередь"})

    @router.post("/telegram/materials/{material_id}/analyze",status_code=202)
    def telegram_analyze(material_id:str):
        enabled()
        from ..research.telegram import TelegramFeed
        job=TelegramFeed(s,settings).queue(runner,material_id,manual=True)
        return result({"job_id":job["id"] if job else None,"cached":job is None})

    @router.post("/telegram/digest",status_code=202)
    def telegram_digest():
        enabled()
        from ..research.telegram import TelegramFeed
        TelegramFeed(s,settings).check(True)
        job=runner.enqueue("telegram_digest",key="scheduled")
        return result({"job_id":job["id"]})

    @router.get("/materials/{material_id}")
    def material(material_id:str,as_of:int|None=None,revision_id:str|None=None):
        with db.read_tx():
            return result(s.material(material_id,as_of,revision_id))

    @router.get("/identity/candidates")
    def identity_candidates():
        return result({"items":s.identity_candidates()})

    @router.get("/identity/instruments")
    def identity_instruments():
        return result({"items":[{"instrument_id":i.id,"link":s.instrument_link(i.id)} for i in db.get_instruments()]})

    @router.post("/identity/candidates/{identifier}/resolve")
    def identity_resolve(identifier:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("resolve_identity:"+identifier,key,body,lambda:s.resolve_identity(identifier,body))

    @router.get("/integration/status")
    def status():
        return result(runner.integration.status())

    @router.post("/integration/import-batch")
    def import_batch(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        enabled()
        if len(encode(body).encode("utf-8"))>4*1024*1024:raise ResearchError("import_limit","Страница импорта не более 4 МиБ",413)
        captured=timestamp(body.get("archive_captured_at"),"archive_captured_at",optional=False,future=False)
        dataset=body.get("dataset_id","")
        if not isinstance(dataset,str) or not dataset.startswith("archive:") or len(dataset)>100:raise ResearchError("invalid_dataset","Для ручного импорта нужен ID archive:<sha256>")
        state={"dataset_id":dataset,"source_kind":"archive","archive_captured_at":captured,"last_import_at":s.clock(),"snapshot_watermark":"archive:complete"}
        def apply():
            count,bad,updated=runner.integration.page(body,state=state,snapshot=True)
            updated["last_success_at"]=captured;updated["last_import_at"]=s.clock();s.set_state("terminal",updated)
            return {"imported":count,"quarantined":bad,"archive_captured_at":captured}
        return mutate("archive_import",key,body,apply)

    @router.post("/integration/sync",status_code=202)
    def sync():
        job = runner.enqueue("sync")
        return result({"job_id":job["id"],"status":job["status"]})

    @router.get("/integration/quarantine")
    def quarantine():
        return result({"items":s.rows("SELECT id,entity_kind,entity_id,revision_id,reason,available_at,status "
                                      "FROM research_quarantine WHERE status='pending' ORDER BY available_at,id LIMIT 100")})

    @router.post("/integration/quarantine/{identifier}/retry",status_code=202)
    def retry_quarantine(identifier:str):
        job = runner.enqueue("quarantine",{"id":identifier},key=identifier)
        return result({"job_id":job["id"]})

    @router.get("/jobs/{identifier}")
    def job(identifier:str):
        return result(runner.job(identifier))

    @router.post("/jobs/{identifier}/cancel")
    def cancel(identifier:str):
        enabled()
        return result(runner.cancel(identifier))

    @router.get("/catalysts/source-status")
    def source_status():
        return result({"terminal":runner.integration.status(),
                       "deribit":{"enabled":settings.research_deribit_enabled,"BTC":s.state("deribit:BTC"),"ETH":s.state("deribit:ETH")}})

    @router.post("/catalysts/collect",status_code=202)
    def collect():
        job = runner.enqueue("collect")
        return result({"job_id":job["id"],"status":job["status"]})

    @router.post("/catalysts/merge")
    def merge(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("merge",key,body,lambda:catalysts.merge(body))

    @router.post("/catalysts/similar")
    def similar(body:dict=Body(...)):
        return result(catalysts.similar(body))

    @router.get("/catalyst-candidates")
    def candidates(status:str="pending",cursor:str|None=None,limit:int=Query(50,ge=1,le=100)):
        return result(catalysts.candidates(status,cursor,limit))

    @router.post("/catalyst-candidates/{identifier}/review")
    def review(identifier:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("review:"+identifier,key,body,lambda:catalysts.review(identifier,body))

    @router.get("/catalysts")
    def list_catalysts(from_at:int|None=Query(None,alias="from"),to_at:int|None=Query(None,alias="to"),
                       asset_uid:str|None=None,scope:str|None=None,kind:str|None=None,stage:str|None=None,
                       review_status:str|None="accepted",unscheduled:bool=False,cursor:str|None=None,
                       limit:int=Query(50,ge=1,le=100),as_of:int|None=None,include_global:bool=False):
        with db.read_tx():
            return result(catalysts.list(from_at=from_at,to_at=to_at,asset_uid=asset_uid,scope=scope,kind=kind,
                          stage=stage,review_status=review_status,unscheduled=unscheduled,after=cursor,limit=limit,
                          as_of=as_of,include_global=include_global))

    @router.post("/catalysts")
    def create_catalyst(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("create_catalyst",key,body,lambda:catalysts.save(body,reason=body.get("reason","Создано вручную")))

    @router.get("/catalysts/{identifier}")
    def catalyst(identifier:str,as_of:int|None=None):
        return result(catalysts.get(identifier,as_of))

    @router.patch("/catalysts/{identifier}")
    def update_catalyst(identifier:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("update:"+identifier,key,body,lambda:catalysts.save(
            {**catalysts.get(identifier),**body},identifier,body.get("expected_revision"),body.get("reason","Исправлено пользователем")))

    @router.get("/catalysts/{identifier}/history")
    def catalyst_history(identifier:str,cursor:str|None=None,limit:int=Query(50,ge=1,le=100)):
        return result(catalysts.history(identifier,cursor,limit))

    @router.post("/catalysts/{identifier}/split")
    def split(identifier:str,body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("split:"+identifier,key,body,lambda:catalysts.split(identifier,body))

    @router.get("/subscriptions")
    def subscriptions():
        return result({"items":runner.reminders.subscriptions()})

    @router.get("/channels")
    def channels():
        from ..research.channels import Channels
        return result({"items":Channels(s).list()})

    @router.post("/channels")
    def configure_channel(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        from ..research.channels import Channels
        return mutate("configure_channel",key,body,lambda:Channels(s).save(body))

    @router.post("/channels/{handle}/collect",status_code=202)
    def collect_channel(handle:str):
        from ..research.channels import Channels
        if not any(c["handle"]==handle and c["enabled"] for c in Channels(s).list()):
            raise ResearchError("channel_disabled","Канал не подключён или выключен",503)
        job=runner.enqueue("channel",{"handle":handle},key=handle)
        return result({"job_id":job["id"]})

    @router.delete("/channels/{handle}")
    def remove_channel(handle:str):
        from ..research.accounts import normalize_handle
        enabled();handle=normalize_handle(handle,'telegram')
        with s.db.batch_writes():
            s.execute("DELETE FROM research_channel WHERE handle=?",(handle,))
            s.audit('channel',handle,{'action':'remove'})
        return result({'removed':True})

    @router.get("/x-accounts")
    def x_accounts():
        from ..research.accounts import XAccounts
        return result({'items':XAccounts(s).list(),'configured':XAccounts(s).configured()})

    @router.post("/x-accounts")
    def save_x_account(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        from ..research.accounts import XAccounts
        return mutate('configure_x_account',key,body,lambda:XAccounts(s).save(body))

    @router.delete("/x-accounts/{handle}")
    def remove_x_account(handle:str):
        from ..research.accounts import XAccounts
        enabled();return result(XAccounts(s).remove(handle))

    @router.post("/x-accounts/{handle}/collect",status_code=202)
    def collect_x_account(handle:str):
        from ..research.accounts import XAccounts,normalize_handle
        enabled();handle=normalize_handle(handle,'x')
        if not s.state('x-account:'+handle).get('enabled'):raise ResearchError('account_disabled','Аккаунт выключен или удалён',503)
        if not XAccounts(s).configured():raise ResearchError('x_not_configured','Укажите токен X в настройках импорта',503)
        return result({'job_id':runner.enqueue('x_account',{'handle':handle},key=handle)['id']})

    @router.post("/subscriptions")
    def subscribe(body:dict=Body(...),key:str|None=Header(None,alias="Idempotency-Key")):
        return mutate("subscribe",key,body,lambda:runner.reminders.subscribe(body))

    @router.delete("/subscriptions/{identifier}")
    def unsubscribe(identifier:str):
        enabled()
        return result(runner.reminders.disable(identifier))

    from .research_workbench_api import register as register_workbench
    register_workbench(router,runner,settings,result,mutate)
    app.include_router(router)
    return runner
