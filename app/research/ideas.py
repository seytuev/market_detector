"""Research theses with immutable evidence, manual verdicts and price episodes."""
import math
from .contracts import ResearchError,choice,text,timestamp,uid,encode,decode,digest,quote_supported
from .store import local_id
from .records import Records
from .market import Market,HOUR,DAY
from .terminal_bridge import execute as terminal

TRANSITIONS={"draft":{"watching","archived"},"watching":{"supported","disputed","resolved","invalidated","archived"},
 "supported":{"watching","disputed","resolved","invalidated","archived"},"disputed":{"watching","supported","resolved","invalidated","archived"},
 "resolved":{"watching","archived"},"invalidated":{"watching","archived"},"archived":{"draft","watching"}}


class Ideas:
    def __init__(self,store):self.s=store;self.r=Records(store);self.market=Market(store)

    def technical(self,refs,asset_uid):
        if not isinstance(refs,list) or len(refs)>10:raise ResearchError("technical_limit","Не более 10 технических связей")
        result=[]
        for ref in refs:
            kind=choice(ref.get("kind"),{"htf_idea","ltf_scenario"},"technical.kind")
            try:identifier=int(ref.get("id"))
            except (ValueError,TypeError):raise ResearchError("invalid_reference","Неверный ID технической связи")
            if kind=="htf_idea":row=self.s.one("SELECT * FROM htf_idea WHERE id=?",(identifier,))
            else:row=self.s.one("SELECT s.*,o.instrument_id FROM ltf_scenario s JOIN ltf_observation o ON o.id=s.observation_id WHERE s.id=?",(identifier,))
            if not row:raise ResearchError("technical_not_found","Технический объект не найден",422)
            link=self.s.instrument_link(row["instrument_id"])
            if not link or link.get("asset_uid")!=asset_uid:
                if ref.get("relation")!="contextual" or len(ref.get("explanation",""))<10:
                    raise ResearchError("asset_mismatch","Техническая связь относится к другому активу",422)
            semantic={key:value for key,value in row.items() if key not in {"updated_at","last_processed_close"}}
            result.append({"kind":kind,"id":identifier,"instrument_id":row["instrument_id"],"relation":ref.get("relation","direct"),
                           "explanation":ref.get("explanation",""),"snapshot":row,"snapshot_hash":digest(semantic),"available_at":self.s.clock()})
        return result

    def validate(self,body,prior=None):
        status=choice(body.get("status","draft"),set(TRANSITIONS),"status")
        asset=body.get("primary_asset_uid") or None
        if asset:self.s.require_asset(asset)
        criteria=body.get("criteria",[])
        if not isinstance(criteria,list) or len(criteria)>30:raise ResearchError("criteria_limit","Не более 30 критериев")
        validated=[]
        for c in criteria:
            if not isinstance(c,dict):raise ResearchError("invalid_criterion","Критерий должен быть структурированным условием")
            evaluator=choice(c.get("evaluator","manual"),{"manual","date","catalyst_stage","technical_transition"},"evaluator")
            params=c.get("parameters",{})
            if not isinstance(params,dict):raise ResearchError("invalid_condition","Параметры критерия должны быть объектом")
            if evaluator=="date":timestamp(params.get("at"),"criterion.at",optional=False)
            if evaluator=="catalyst_stage":
                from .catalysts import Catalysts
                Catalysts(self.s).get(params.get("id",""));choice(params.get("stage"),{"rumor","proposal","announced","live","denied","cancelled"},"criterion.stage")
            if evaluator=="technical_transition":
                choice(params.get("kind"),{"htf_idea","ltf_scenario"},"criterion.kind")
                text(params.get("state"),"criterion.state",1,80)
                self.technical([{"kind":params["kind"],"id":params.get("id")}],asset)
            validated.append({"id":c.get("id") or uid(),"type":choice(c.get("type"),{"confirmation","invalidation","review_trigger"},"criterion.type"),
                "description":text(c.get("description"),"criterion.description",5,2000),"evaluator":evaluator,"parameters":params})
        thesis=text(body.get("thesis",""),"thesis",0 if status=="draft" else 20,8000)
        mechanism=text(body.get("mechanism",""),"mechanism",0 if status=="draft" else 10,4000)
        due=timestamp(body.get("review_due_at"),"review_due_at")
        review_condition=text(body.get("review_condition",""),"review_condition",0,1000)
        if status not in {"draft","archived"}:
            if not asset:raise ResearchError("asset_required","Для наблюдения выберите подтверждённый актив")
            if not any(c["type"]=="invalidation" for c in validated):raise ResearchError("invalidation_required","Нужен критерий опровержения")
            if due is None and not review_condition and not any(c["type"]=="review_trigger" for c in validated):
                raise ResearchError("review_required","Укажите дату или условие пересмотра")
        refs=body.get("source_refs",[]); snapshots=self.r.sources(refs)
        technical_refs=body.get("technical_refs",[])
        # Reuse frozen snapshots when an ordinary text edit leaves the same links.
        if prior and technical_refs==prior.get("technical_refs",[]):technical=prior.get("technical_snapshots",[])
        else:technical=self.technical(technical_refs,asset)
        instrument_id=body.get("instrument_id")
        if instrument_id:
            self.market.instrument(instrument_id)
            link=self.s.instrument_link(instrument_id)
            if not link or link.get("asset_uid")!=asset:raise ResearchError("asset_mismatch","Инструмент не связан с активом идеи",422)
        return {"title":text(body.get("title"),"title",5,200),"primary_asset_uid":asset,"related_assets":body.get("related_assets",[]),
            "thesis":thesis,"mechanism":mechanism,"direction":choice(body.get("direction","watch"),{"positive","negative","neutral","watch"},"direction"),
            "status":status,"criteria":validated,"review_due_at":due,"review_condition":review_condition,"horizon_label":text(body.get("horizon_label",""),"horizon_label",0,200),
            "source_refs":refs,"source_snapshots":snapshots,"technical_refs":technical_refs,"technical_snapshots":technical,
            "author_note":text(body.get("author_note",""),"author_note",0,4000),"provenance":(prior or {}).get("provenance","manual"),
            "instrument_id":instrument_id,"copied_from_id":body.get("copied_from_id"),"final_status":body.get("final_status"),
            "external_origin":(prior or {}).get("external_origin"),"external_revision_hash":(prior or {}).get("external_revision_hash"),
            "model_metadata":(prior or {}).get("model_metadata"),"ai_claims":(prior or {}).get("ai_claims"),"memory_metadata":(prior or {}).get("memory_metadata")}

    def import_external(self,body):
        external=text(body.get("external_id"),"external_id",1,200);raw=body.get("idea")
        if not isinstance(raw,dict):raise ResearchError("invalid_import","Нужна исходная идея STVTerminal")
        native=terminal("external_idea",value={**raw,"title":raw.get("title") or raw.get("ticker","")+" · импорт"})
        identifier=local_id("stvterminal","research-idea",external);checksum=digest(raw)
        previous=self.s.one("SELECT payload FROM research_object WHERE id=? AND kind='idea'",(identifier,))
        prior=decode(previous["payload"]) if previous else None
        if prior and prior.get("external_revision_hash")==checksum:return prior
        if prior and prior["actor"]!="stvterminal":
            self.r.note(identifier,"external_conflict",{"external_revision_hash":checksum,"external_snapshot":native,"local_revision_id":prior["revision_id"]},key=checksum)
            return {"id":identifier,"conflict":True,"local_revision_id":prior["revision_id"]}
        namespace="cmc" if native["marketSource"]=="coinmarketcap" else native["marketSource"]
        external_asset=native.get("marketSourceId") or native.get("assetRef");asset=None
        if external_asset:
            row=self.s.one("SELECT asset_uid FROM research_asset_external_id WHERE namespace=? AND external_id=?",(namespace,external_asset))
            if row:asset=row["asset_uid"]
            else:asset=self.s.create_asset({"display_symbol":native["ticker"],"name":native.get("assetName") or native["ticker"],
                "external_ids":[{"namespace":namespace,"external_id":external_asset}]},actor="stvterminal")["asset_uid"]
        display_title=native["title"] if len(native["title"])>=5 else "Идея · "+native["title"]
        value=self.validate({"title":display_title,"primary_asset_uid":asset,"status":"draft","thesis":native.get("thesis") or native.get("body","")[:8000],
            "mechanism":"","criteria":[{"type":"invalidation","description":native["invalidation"],"evaluator":"manual"}] if len(native.get("invalidation",""))>=5 else [],
            "author_note":native.get("body","")[:4000]},prior)
        value.update({"provenance":"imported","external_origin":{"provider":"stvterminal","id":external,"created_at":raw.get("createdAt"),"source_observed_at":raw.get("updatedAt"),
            "baseline":{"price":raw.get("signalPrice"),"observed_at":raw.get("signalAt"),"source":native["marketSource"],"policy":"external_archive_only"},"snapshot":native},"external_revision_hash":checksum})
        with self.s.db.batch_writes():return self.r.save("idea",value,identifier if prior else None,prior["revision_id"] if prior else None,
            "Импортирована идея STVTerminal как черновик",actor="stvterminal",new_id=identifier)

    def ai_draft(self,body,model=None,memory_enabled=False):
        from .model import Model
        refs=body.get("source_refs",[])
        if not refs:raise ResearchError("sources_required","Для ИИ-черновика нужны первоисточники")
        sources=self.r.sources(refs);asset=body.get("primary_asset_uid")
        if asset:self.s.require_asset(asset)
        schema={"type":"object","additionalProperties":False,"required":["title","thesis","mechanism","confirmation","invalidation","review_condition","claims"],"properties":{
            **{key:{"type":"string"} for key in ("title","thesis","mechanism","review_condition")},
            "confirmation":{"type":"array","items":{"type":"string"}},"invalidation":{"type":"array","items":{"type":"string"}},
            "claims":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["text","quote","source_revision_id"],
                "properties":{key:{"type":"string"} for key in ("text","quote","source_revision_id")}}}}}
        payload={"asset":self.s.asset(asset) if asset else None,"sources":sources}
        if memory_enabled and body.get("section_id") and body.get("dataset_id"):
            from .playbook import Playbook
            book=Playbook(self.s);query=book.source_snapshot(refs[0]);payload["historical_examples"]=book.memory(body["section_id"],body["dataset_id"],query)
        response=(model or Model(self.s)).call("idea_draft",payload,strong=True,spec={"schema":{"name":"research_idea_draft","schema":schema},"prompt":
            "Write a Russian research hypothesis draft based only on the supplied frozen sources. Explain a possible mechanism as a hypothesis, not a fact or a trading verdict. "
            "Every factual claim must include an exact original quotation and its source revision ID. Keep unsupported points as conditions to verify. "
            "Provide falsifiable confirmation and invalidation conditions and a review condition. Historical labelled examples describe research style only; never copy their old facts, prices or direction."})
        value=response["output"];by_revision={source["ref"]["revision_id"]:source["snapshot"] for source in sources}
        claims=value.get("claims",[])
        if not isinstance(claims,list) or len(claims)>30 or any(not isinstance(c,dict) or not isinstance(c.get("quote"),str) for c in claims):
            raise ResearchError("invalid_model_json","Не удалось проверить цитаты ИИ-черновика",502)
        for claim in claims:
            source=by_revision.get(claim.get("source_revision_id"));original=(source or {}).get("original_text") or (source or {}).get("summary") or (source or {}).get("thesis","")
            if not source or not quote_supported(claim.get("quote",""),original):raise ResearchError("unverified_claim","ИИ-черновик содержит утверждение без точной исходной цитаты",502)
        result=self.validate({"title":value.get("title"),"primary_asset_uid":asset,"status":"draft","thesis":value.get("thesis"),"mechanism":value.get("mechanism"),
            "source_refs":refs,"review_condition":value.get("review_condition"),"criteria":[{"type":kind,"description":description,"evaluator":"manual"} for key,kind in (("confirmation","confirmation"),("invalidation","invalidation")) for description in value.get(key,[])[:10]]})
        result.update({"provenance":"ai_draft","model_metadata":response["usage"],"ai_claims":value.get("claims",[]),"memory_metadata":payload.get("historical_examples")})
        return self.r.save("idea",result,reason="Пользователь запросил ИИ-черновик по проверенным источникам",actor="model")

    def save(self,body,identifier=None):
        prior=self.r.get("idea",identifier) if identifier else None
        merged={**(prior or {}),**body}
        if prior and merged.get("status")!=prior["status"]:raise ResearchError("transition_required","Смените состояние отдельным действием")
        if not prior and merged.get("status","draft") not in {"draft","watching"}:raise ResearchError("invalid_transition","Новая идея начинается как черновик или наблюдение")
        value=self.validate(merged,prior)
        with self.s.db.batch_writes():
            result=self.r.save("idea",value,identifier,body.get("expected_revision"),body.get("reason","Создана исследовательская идея"))
            if not prior and value["status"]=="watching":self.start_episode(result)
        return result

    def transition(self,identifier,body):
        prior=self.r.get("idea",identifier); target=body.get("status")
        if target not in TRANSITIONS[prior["status"]]:raise ResearchError("invalid_transition","Переход состояния не разрешён",422)
        reason=text(body.get("reason"),"reason",5,1000)
        if target in {"resolved","invalidated"} and not (body.get("evidence_refs") or body.get("criterion_id") or body.get("manual_decision") is True):
            raise ResearchError("conclusion_required","Укажите свидетельство, критерий или явное ручное решение")
        value=self.validate({**prior,"status":target,"final_status":prior["status"] if target=="archived" else prior.get("final_status")},prior)
        with self.s.db.batch_writes():
            result=self.r.save("idea",value,identifier,body.get("expected_revision"),reason)
            self.r.note(identifier,"transition",{"from":prior["status"],"to":target,"reason":reason,"evidence":self.r.sources(body.get("evidence_refs",[])),
                "manual_decision":bool(body.get("manual_decision")),"criterion_id":body.get("criterion_id")},result["revision_id"])
            if target=="watching" and prior["status"] in {"draft","resolved","invalidated","archived"}:self.start_episode(result)
        return result

    def review(self,identifier,body):
        prior=self.r.get("idea",identifier)
        if body.get("expected_revision")!=prior["revision_id"]:raise ResearchError("revision_conflict","Версия идеи изменилась",409)
        verdict=choice(body.get("verdict"),{"supports","weakens","refutes","inconclusive"},"verdict")
        evidence=self.r.sources(body.get("evidence_refs",[]))
        payload={"reviewed_revision_id":prior["revision_id"],"verdict":verdict,"explanation":text(body.get("explanation"),"explanation",10,4000),"evidence":evidence}
        with self.s.db.batch_writes():
            result=self.r.note(identifier,"review",payload,prior["revision_id"],body.get("request_key"))
            if body.get("trigger_id"):
                self.r.note(identifier,"trigger_resolved",{"trigger_id":body["trigger_id"],"review_id":result["id"]},key=body["trigger_id"])
        return result

    def add_link(self,identifier,body):
        prior=self.r.get("idea",identifier)
        return self.save({"source_refs":[*prior["source_refs"],body["source_ref"]],"expected_revision":body.get("expected_revision"),"reason":body.get("reason")},identifier)

    def copy(self,identifier,body):
        prior=self.r.get("idea",identifier)
        value=self.validate({**prior,"title":body.get("title",prior["title"]+" — копия"),"status":"draft","copied_from_id":identifier},prior)
        value["provenance"]="copy"
        return self.r.save("idea",value,reason="Создана копия исследовательской идеи")

    def start_episode(self,idea):
        instrument=idea.get("instrument_id")
        if not instrument:
            resolved=self.market.resolve(idea["primary_asset_uid"]);instrument=resolved["id"] if resolved else None
        episode={"id":uid(),"started_at":self.s.clock(),"idea_revision_id":idea["revision_id"],"instrument_id":instrument,
                 "quality":"pending" if instrument else "unavailable","reason":"waiting_quote" if instrument else "unresolved_instrument",
                 "method_version":"quote_to_h1_close_v1","baseline":None}
        self.r.note(idea["id"],"episode",episode,idea["revision_id"],episode["id"])
        self.collect(idea["id"])

    def collect(self,identifier):
        episodes=self.r.notes(identifier,"episode",limit=100)["items"]
        moment=self.s.clock()
        for episode in episodes:
            key=episode["id"]
            state=self.s.state("idea-episode:"+key) or episode
            ins=state.get("instrument_id")
            if not ins:continue
            if state.get("baseline") is None and state["quality"]=="pending":
                q=self.s.db.get_quote_detail(ins)
                observed=(q.get("event_at") or q.get("ts")) if q else None
                if q and observed is not None and state["started_at"]<=observed<=state["started_at"]+900000 and q["received_at"]<=state["started_at"]+900000 and math.isfinite(q["price"]) and q["price"]>0:
                    state={**state,"quality":"ok","reason":None,"baseline":{**q,"observed_at":observed,"instrument_id":ins,"quote_asset":"USDT"}}
                elif moment>state["started_at"]+900000:state={**state,"quality":"unavailable","reason":"quote_window_missed"}
                self.s.set_state("idea-episode:"+key,state)
            base=state.get("baseline")
            if not base:continue
            for days in (1,3,7,30):
                target=base["observed_at"]+days*DAY
                if target>moment:continue
                boundary=((target+HOUR-1)//HOUR)*HOUR
                bar=self.market.price_at(ins,boundary)
                if not bar and boundary+HOUR<=target+HOUR:bar=self.market.price_at(ins,boundary+HOUR)
                reason="pending" if moment<target+HOUR else "missing"
                result={"episode_id":key,"days":days,"target_at":target,"actual_at":bar["close_boundary"] if bar else None,
                    "price":bar["close"] if bar else None,"return_pct":(bar["close"]/base["price"]-1)*100 if bar else None,
                    "quality":"ok" if bar else reason,"baseline":base,"benchmark_return_pct":None,"excess_return_pct":None,
                    "method_version":"quote_to_h1_close_v1"}
                benchmark=self.market.benchmark()
                if bar and benchmark:
                    b0=self.s.one("SELECT payload FROM research_series WHERE kind='idea_benchmark' AND object_id=? ORDER BY available_at,id LIMIT 1",(key,))
                    b1=self.market.price_at(benchmark,bar["close_boundary"])
                    if b0 and b1:
                        b0=decode(b0["payload"]);result["benchmark_return_pct"]=(b1["close"]/b0["price"]-1)*100;result["excess_return_pct"]=result["return_pct"]-result["benchmark_return_pct"]
                checksum=digest(result)
                self.s.execute("INSERT INTO research_series VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,object_id,at_ms,content_hash) DO NOTHING",
                               (uid(),"idea_outcome",key,target,moment,checksum,encode(result)))
            benchmark=self.market.benchmark();q=self.s.db.get_quote_detail(benchmark) if benchmark else None
            if q and (q.get("event_at") or q.get("ts"))==base["observed_at"] and q["received_at"]<=state["started_at"]+900000:
                self.s.execute("INSERT INTO research_series VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,object_id,at_ms,content_hash) DO NOTHING",
                               (uid(),"idea_benchmark",key,base["observed_at"],moment,digest(q),encode(q)))

    def outcomes(self,identifier):
        self.r.get("idea",identifier);episodes=self.r.notes(identifier,"episode",limit=100)["items"]
        result=[]
        for episode in episodes:
            state=self.s.state("idea-episode:"+episode["id"]) or episode
            rows=self.s.rows("SELECT payload FROM research_series r WHERE kind='idea_outcome' AND object_id=? AND NOT EXISTS(SELECT 1 FROM research_series n "
                "WHERE n.kind=r.kind AND n.object_id=r.object_id AND n.at_ms=r.at_ms AND (n.available_at>r.available_at OR (n.available_at=r.available_at AND n.id>r.id))) ORDER BY at_ms",(episode["id"],))
            outcomes=[decode(row["payload"]) for row in rows]
            metrics=None
            if state.get("baseline"):
                metrics=terminal("idea_metrics",snapshots=[{"price":o["price"],"capturedAt":o["actual_at"],"marketCap":None,"volume24h":None} for o in outcomes if o["price"] is not None],
                    price=state["baseline"]["price"],now=self.s.clock(),started_at=state["started_at"])
            result.append({**state,"outcomes":outcomes,"native_metrics":metrics})
        return {"items":result,"description":"Описательная динамика цены; вывод по тезису фиксируется отдельным пересмотром."}

    def check_reviews(self,identifier):
        idea=self.r.get("idea",identifier)
        if idea["status"] not in {"watching","supported","disputed"}:return
        if idea.get("review_due_at") and idea["review_due_at"]<=self.s.clock():
            self.r.note(identifier,"review_trigger",{"type":"due","idea_revision_id":idea["revision_id"]},key="due:"+idea["revision_id"])
        for ref in idea.get("source_refs",[]):
            if ref["kind"]!="catalyst":continue
            from .catalysts import Catalysts
            current=Catalysts(self.s).get(ref["id"])
            if current["revision_id"]!=ref["revision_id"]:
                self.r.note(identifier,"review_trigger",{"type":"catalyst_changed","before":ref,"after":{"kind":"catalyst","id":current["id"],"revision_id":current["revision_id"]}},key=idea["revision_id"]+":"+current["revision_id"])
        for snap in idea.get("technical_snapshots",[]):
            try:current=self.technical([{"kind":snap["kind"],"id":snap["id"],"relation":snap["relation"],"explanation":snap["explanation"]}],idea["primary_asset_uid"])[0]
            except ResearchError:
                current={"snapshot_hash":"unavailable","snapshot":None}
            if current["snapshot_hash"]!=snap["snapshot_hash"]:
                self.r.note(identifier,"review_trigger",{"type":"technical_changed" if current["snapshot"] else "data_unavailable","before":snap,"after":current},key=idea["revision_id"]+":"+current["snapshot_hash"])
        for criterion in idea["criteria"]:
            p=criterion["parameters"];trigger=False;evidence=None
            if criterion["evaluator"]=="date":trigger=p["at"]<=self.s.clock();evidence={"at":p["at"]}
            if criterion["evaluator"]=="catalyst_stage":
                from .catalysts import Catalysts
                event=Catalysts(self.s).get(p["id"]);trigger=event["stage"]==p["stage"];evidence={"kind":"catalyst","id":event["id"],"revision_id":event["revision_id"]}
            if criterion["evaluator"]=="technical_transition":
                try:
                    current=self.technical([{"kind":p["kind"],"id":p["id"]}],idea["primary_asset_uid"])[0]
                    state=current["snapshot"].get("state") if p["kind"]=="ltf_scenario" else "closed" if current["snapshot"].get("manual_closed_at") else "active"
                    trigger=state==p["state"];evidence={"kind":p["kind"],"id":p["id"],"snapshot_hash":current["snapshot_hash"],"state":state}
                except ResearchError:
                    self.r.note(identifier,"review_trigger",{"type":"data_unavailable","criterion_id":criterion["id"]},key=idea["revision_id"]+":"+criterion["id"]+":unavailable")
            if trigger:self.r.note(identifier,"review_trigger",{"type":"criterion","criterion":criterion,"evidence":evidence},key=idea["revision_id"]+":"+criterion["id"]+":"+digest(evidence))

    def maintenance(self,limit=50):
        # Bounded batches, rotating through active objects to avoid starving older theses.
        after=self.s.state("ideas-maintenance").get("after","")
        rows=self.s.rows("SELECT id FROM research_object WHERE kind='idea' AND status IN ('watching','supported','disputed') AND id>? ORDER BY id LIMIT ?",(after,limit))
        for row in rows:
            try:self.collect(row["id"]);self.check_reviews(row["id"])
            except ResearchError as error:self.s.set_state("idea-error:"+row["id"],{"error_code":error.code,"at":self.s.clock()})
        self.s.set_state("ideas-maintenance",{"after":rows[-1]["id"] if len(rows)==limit else ""})
