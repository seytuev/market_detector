"""Domain job dispatch kept separate from the technical market calculation queue."""
from .contracts import ResearchError,decode,encode
from .ideas import Ideas
from .strength import Strength
from .baskets import Baskets
from .playbook import Playbook
from .lab import Lab
from .market import DAY

KINDS={"strength","correlations","idea_refresh","idea_draft","basket_observe","playbook_distill","playbook_evaluate","playbook_backfill","playbook_retry","model_probe","lab_prepare","lab_run","lab_sweep"}


def dispatch(runner,kind,payload):
    s=runner.s;cfg=runner.settings
    flag=("strength" if kind in {"strength","correlations"} else "ideas" if kind.startswith("idea_") else "baskets" if kind=="basket_observe" else "playbook" if kind.startswith("playbook") or kind=="model_probe" else "lab")
    if not getattr(cfg,"research_"+flag+"_enabled"):raise ResearchError("disabled","Сервис выключен",503)
    stop=runner.should_stop
    if kind=="strength":
        snap=Strength(s).calculate(payload.get("instrument_ids"),payload.get("benchmark_instrument_id"));return {"snapshot_id":snap["id"],"rows":len(snap["items"])}
    if kind=="correlations":
        snap=Strength(s).correlations(payload.get("instrument_ids",[]));return {"snapshot_id":snap["id"]}
    if kind=="idea_refresh":
        Ideas(s).collect(payload["id"]);Ideas(s).check_reviews(payload["id"]);return {"idea_id":payload["id"]}
    if kind=="idea_draft":
        value=Ideas(s).ai_draft(payload,memory_enabled=cfg.research_playbook_memory_enabled);return {"idea_id":value["id"]}
    if kind=="basket_observe":return Baskets(s).observe(payload["id"])
    if kind=="lab_prepare":
        data=Lab(s).prepare(payload["experiment_id"],payload.get("revision_id"));return {"dataset_id":data["id"],"coverage":data["coverage"],"rows":data["rows"]}
    if kind=="lab_run":return {"run":Lab(s).run(payload["id"],stop)["id"]}
    if kind=="lab_sweep":return Lab(s).execute_sweep(payload["id"],stop)
    book=Playbook(s)
    if kind=="model_probe":
        response=book.model.call("match",{"post":None,"patterns":[],"instruction":"Connectivity check. Return an empty matches array. No research verdict is requested."})
        if response["output"].get("matches")!=[]:raise ResearchError("model_schema_failed","Провайдер не выполнил структурированный запрос",502)
        return {"connected":True,"usage":response["usage"]}
    if kind=="playbook_distill":
        rule=book.distill(payload["id"],payload["dataset_id"]);return {"rule_id":rule["id"],"revision_id":rule["revision_id"]}
    if kind=="playbook_evaluate":
        report=book.evaluate(payload["id"],payload["dataset_id"],payload.get("partition","holdout"),stop,max_new_attempts=5,retry_key=payload.get("retry_key"))
        return {"evaluation_id":report["id"],"status":report["status"],"more_work":report["status"]=="running"}
    if kind=="playbook_retry":
        match=book.r.get("match",payload["id"]);rule=book.r.get("rule",match["rule_id"],match["rule_revision_id"])
        if rule["retrieval_method"]=="model" and not cfg.research_playbook_matching_enabled:raise ResearchError("matching_disabled","Модельное сопоставление выключено",503)
        value=book.retry(payload["id"],stop);return {"match_id":value["id"],"decision":value["decision"]}
    if kind=="playbook_backfill":
        rule=book.r.get("rule",payload["id"])
        if rule["retrieval_method"]=="model" and not cfg.research_playbook_matching_enabled:raise ResearchError("matching_disabled","Модельное сопоставление выключено",503)
        return book.backfill(payload["id"],payload["from_at"],payload["to_at"],payload.get("mode","backfill"),stop)
    raise ResearchError("unknown_job","Неизвестное исследовательское задание")


def maintenance(runner):
    s=runner.s;cfg=runner.settings;now=s.clock()
    if cfg.research_ideas_enabled:Ideas(s).maintenance()
    if cfg.research_baskets_enabled:Baskets(s).maintenance()
    if cfg.research_strength_enabled:
        strength=Strength(s);benchmark=strength.market.benchmark();last=s.db.last_candle(benchmark,"D1") if benchmark else None
        state=s.state("strength-schedule");revision=s.db.latest_candle_revision_at(benchmark,"D1") if benchmark else 0
        signature=[last.open_time if last else None,revision]
        if signature!=state.get("signature") or now-state.get("last_attempt_at",0)>DAY:
            runner.enqueue("strength",key="scheduled");s.set_state("strength-schedule",{"signature":signature,"last_attempt_at":now})
    if cfg.research_playbook_enabled:
        rows=s.rows("SELECT payload FROM research_object WHERE kind='rule' AND status IN ('shadow','active') ORDER BY updated_at,id LIMIT 100")
        for row in rows:
            rule=decode(row["payload"])
            if rule["retrieval_method"]=="model" and not cfg.research_playbook_matching_enabled:continue
            state=s.state("rule-live:"+rule["id"])
            if now-state.get("scheduled_at",0)<300000:continue
            runner.enqueue("playbook_backfill",{"id":rule["id"],"from_at":now-DAY,"to_at":now,"mode":"live"},key="live:"+rule["id"])
            s.set_state("rule-live:"+rule["id"],{"scheduled_at":now})
