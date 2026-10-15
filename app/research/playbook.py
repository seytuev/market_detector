"""Immutable labelled examples, native lexical retrieval and isolated model matching."""
from urllib.parse import urlsplit,urlunsplit
import math
from itertools import zip_longest
from .contracts import ResearchError,choice,text,timestamp,uid,encode,decode,digest
from .records import Records
from .terminal_bridge import execute as terminal
from .model import Model
from .market import DAY

CONDITIONS={"source_in","author_in","explicit_asset","asset_in_watchlist","catalyst_kind","catalyst_stage","min_material_freshness","has_verified_quote"}


def retrieval_snapshot(value):
    """Bound native TF-IDF input; the full immutable evidence stays in storage."""
    return {**{key:value.get(key) for key in ("kind","ref","author","at","url","symbol")},
            "text":str(value.get("text") or "")[:4000],"title":str(value.get("title") or "")[:140],
            "summary":str(value.get("summary") or "")[:1500],"tickers":value.get("tickers",[])[:30],
            "images":[],"imageNotes":[str(v)[:400] for v in value.get("imageNotes",[])[:4]]}


def model_example(value):
    source=retrieval_snapshot(value["source_snapshot"])
    source.update(text=source["text"][:700],summary=source["summary"][:600])
    return {"revision_id":value["revision_id"],"label":value["label"],"information_direction":value["information_direction"],
            "explanation":value["explanation"][:600],"focus":value["focus"],"source":source}


def strings(values,name,limit=20):
    if not isinstance(values,list) or len(values)>limit:raise ResearchError("invalid_list","Некорректный список "+name)
    return [text(v,name,1,500) for v in values]


class Playbook:
    def __init__(self,store,model=None):self.s=store;self.r=Records(store);self.model=model or Model(store)

    def section(self,body,identifier=None):
        prior=self.r.get("section",identifier) if identifier else {}
        value={**prior,**body}
        return self.r.save("section",{"title":text(value.get("title"),"title",3,200),"purpose":text(value.get("purpose",""),"purpose",0,4000),
            "track":choice(value.get("track","research"),{"catalyst","narrative","attention","risk","trade_hypothesis","research"},"track"),
            "status":choice(value.get("status","active"),{"active","archived"},"status")},identifier,body.get("expected_revision"),body.get("reason","Создана рубрика учебника"))

    def source_snapshot(self,ref,as_of=None):
        value=self.r.source(ref,as_of);assets=[]
        if ref["kind"]=="material":
            rows=self.s.rows("SELECT asset_uid FROM research_material_asset WHERE revision_id=? AND resolution IN ('manual','verified')",(ref["revision_id"],))
            assets=[r["asset_uid"] for r in rows]
        elif ref["kind"]=="catalyst":assets=value.get("asset_uids",[])
        elif value.get("primary_asset_uid"):assets=[value["primary_asset_uid"]]
        symbols=[self.s.require_asset(a)["display_symbol"] for a in dict.fromkeys(assets)]
        body=value.get("original_text") or value.get("summary") or value.get("thesis") or ""
        group=value.get("origin_material_id") or value.get("canonical_url") or ref["id"]
        if isinstance(group,str) and group.startswith("http"):
            parsed=urlsplit(group);group=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,"",""))
        return {"kind":value.get("source_kind",ref["kind"]),"ref":ref["id"],"revision_id":ref["revision_id"],"url":value.get("canonical_url"),"href":None,
            "author":value.get("author"),"at":value.get("published_at") or value.get("available_at"),"available_at":value["available_at"],"text":body,
            "title":value.get("title",""),"summary":value.get("summary",""),"tickers":symbols,"symbol":symbols[0] if len(symbols)==1 else None,
            "asset_uids":assets,"images":[],"imageNotes":[],"story_group_id":digest([ref["kind"],group]),
            "catalyst_kind":value.get("kind") if ref["kind"]=="catalyst" else None,"catalyst_stage":value.get("stage"),
            "verified_quote":any(c.get("verified") for a in value.get("analyses",[]) for c in a.get("claims",[])) if value.get("analyses") else None,
            "watchlist_at_source":None,"source_ref":ref,"raw_snapshot":value}

    def example(self,body,identifier=None):
        prior=self.r.get("example",identifier) if identifier else {}
        value={**prior,**body};section=self.r.get("section",value.get("section_id"))
        label=choice(value.get("label"),{"positive","negative","partial","uncertain"},"label")
        snapshot=prior.get("source_snapshot") if prior and value["source_ref"]==prior["source_ref"] else self.source_snapshot(value["source_ref"])
        result={"section_id":section["id"],"title":snapshot["title"] or "Пример учебника","source_ref":value["source_ref"],"source_snapshot":snapshot,
            "label":label,"explanation":text(value.get("explanation"),"explanation",20 if label in {"positive","negative"} else 5,4000),
            "focus":choice(value.get("focus","whole"),{"author","wording","event","numbers","chart","asset","mechanism","whole"},"focus"),
            "information_direction":choice(value.get("information_direction","none"),{"positive","negative","neutral","watch","none"},"information_direction"),
            "horizon":text(value.get("horizon",""),"horizon",0,200),"status":"labelled"}
        return self.r.save("example",result,identifier,body.get("expected_revision"),body.get("reason","Добавлен размеченный пример"))

    def conditions(self,values):
        if not isinstance(values,list) or len(values)>20:raise ResearchError("condition_limit","Не более 20 условий")
        result=[]
        for value in values:
            kind=choice(value.get("kind"),CONDITIONS,"condition.kind")
            params=value.get("values",[]) if kind in {"source_in","author_in","catalyst_kind","catalyst_stage"} else value.get("value")
            if isinstance(params,list):params=strings(params,"condition.values")
            if kind=="min_material_freshness":
                if not isinstance(params,(int,float)) or not math.isfinite(params) or not 0<params<=168:raise ResearchError("invalid_condition","Свежесть задаётся часами от 0 до 168")
            result.append({"kind":kind,"mode":choice(value.get("mode","prefer"),{"require","prefer","off"},"condition.mode"),
                           "origin":choice(value.get("origin","manual"),{"manual","proposed"},"condition.origin"),"values":params})
        return result

    def rule(self,body,identifier=None):
        prior=self.r.get("rule",identifier) if identifier else {}
        value={**prior,**body};section=self.r.get("section",value.get("section_id"))
        evidence=value.get("evidence_example_revision_ids",[])
        if len(evidence)>200:raise ResearchError("example_limit","Не более 200 входных примеров правила")
        for revision in evidence:
            row=self.s.one("SELECT r.object_id FROM research_revision r JOIN research_object o ON o.id=r.object_id WHERE r.id=? AND o.kind='example'",(revision,))
            if not row or self.r.get("example",row["object_id"],revision)["section_id"]!=section["id"]:raise ResearchError("invalid_example","Пример относится к другой рубрике")
        if value.get("dataset_id"):
            data=self.r.dataset(value["dataset_id"],"playbook")
            if data["section_id"]!=section["id"]:raise ResearchError("dataset_mismatch","Другая рубрика")
            if set(evidence)-set(data["partitions"]["train"]):raise ResearchError("training_lineage","Примеры правила должны относиться к train")
        result={"section_id":section["id"],"title":section["title"],"text":text(value.get("text"),"text",10,8000),
            "positive_signs":strings(value.get("positive_signs",[]),"positive_signs"),"negative_signs":strings(value.get("negative_signs",[]),"negative_signs"),
            "code_conditions":self.conditions(value.get("code_conditions",[])),"evidence_example_revision_ids":evidence,
            "retrieval_method":choice(value.get("retrieval_method","lexical"),{"lexical","model"},"retrieval_method"),
            "retrieval_threshold":float(value.get("retrieval_threshold",0.3)),"status":"draft","provenance":value.get("provenance","manual"),
            "model_metadata":value.get("model_metadata"),"dataset_id":value.get("dataset_id"),"independence_declaration":bool(value.get("independence_declaration",False))}
        if not math.isfinite(result["retrieval_threshold"]) or not 0<=result["retrieval_threshold"]<=1:raise ResearchError("invalid_threshold","Порог от 0 до 1")
        return self.r.save("rule",result,identifier,body.get("expected_revision"),body.get("reason","Сохранено ручное правило"))

    def dataset(self,section_id):
        self.r.get("section",section_id)
        rows=self.s.rows("SELECT payload FROM research_object WHERE kind='example' AND json_extract(payload,'$.section_id')=? ORDER BY created_at,id LIMIT 10001",(section_id,))
        if len(rows)>10000:raise ResearchError("dataset_limit","Не более 10 000 примеров в снимке")
        examples=[decode(r["payload"]) for r in rows];examples.sort(key=lambda e:(e["available_at"],e["id"]))
        n=len(examples);cut1=examples[min(n-1,int(n*.6))]["available_at"] if n else 0;cut2=examples[min(n-1,int(n*.8))]["available_at"] if n else 0
        groups={}
        for e in examples:groups.setdefault(e["source_snapshot"]["story_group_id"],[]).append(e)
        partitions={"train":[],"validation":[],"holdout":[],"unassigned":[]}
        for group in groups.values():
            stamps=[e["available_at"] for e in group]
            partition="train" if max(stamps)<cut1 else "validation" if min(stamps)>=cut1 and max(stamps)<cut2 else "holdout" if min(stamps)>=cut2 else "unassigned"
            partitions[partition].extend(e["revision_id"] for e in group)
        snapshot=self.r.snapshot("playbook",{"section_id":section_id,"cutoff_at":self.s.clock(),"examples":examples,"partitions":partitions,
            "split_method":"chronological_story_groups_v1","story_groups":{key:[e["revision_id"] for e in values] for key,values in groups.items()},
            "development_only":not all(partitions[k] for k in ("train","validation","holdout"))})
        self.s.set_state("playbook-dataset:"+section_id,{"dataset_id":snapshot["id"]})
        return snapshot

    def distill(self,section_id,dataset_id):
        data=self.r.dataset(dataset_id,"playbook")
        if data["section_id"]!=section_id:raise ResearchError("dataset_mismatch","Другая рубрика")
        train=[e for e in data["examples"] if e["revision_id"] in data["partitions"]["train"]]
        if not train:raise ResearchError("insufficient_train","Нет отдельной обучающей выборки")
        # Same positive/negative bounds as the original distillMessage; retain
        # exact selected revision IDs instead of claiming all train was read.
        section=self.r.get("section",section_id)
        candidates=[e for pair in zip_longest([e for e in train if e["label"]=="positive"][-25:][::-1],[e for e in train if e["label"]=="negative"][-10:][::-1]) for e in pair if e]
        message={"section":{k:section[k] for k in ("title","purpose","track")},"examples":[]};selected=[]
        for example in candidates:
            compact=model_example(example)
            if len(encode({**message,"examples":[*message["examples"],compact]}))>45000:continue
            message["examples"].append(compact);selected.append(example)
        if not selected:raise ResearchError("insufficient_train","Нет размеченных примеров/антипримеров train")
        response=self.model.call("distill",message,strong=True);out=response["output"]
        validated=terminal("rule_validate",value=out,examples=[{"direction":"not_signal" if e["label"]=="negative" else "long" if e["information_direction"]=="positive" else "short" if e["information_direction"]=="negative" else "watch"} for e in train])
        if not validated.get("ok"):raise ResearchError("invalid_model_rule","Модельное правило не прошло исходную проверку STVTerminal",502)
        out=validated["value"]
        old=self.s.one("SELECT payload FROM research_object WHERE kind='rule' AND json_extract(payload,'$.section_id')=? ORDER BY updated_at DESC,id DESC LIMIT 1",(section_id,))
        manual=[c for c in decode(old["payload"]).get("code_conditions",[]) if c["origin"]=="manual"] if old else []
        return self.rule({"section_id":section_id,"text":out.get("summary_ru"),"positive_signs":out.get("signs_ru",[]),"negative_signs":out.get("anti_signs_ru",[]),
            "evidence_example_revision_ids":[e["revision_id"] for e in selected],"dataset_id":dataset_id,"provenance":"model","model_metadata":{**response["usage"],"train_examples":len(train),"selected_examples":len(selected)},"retrieval_method":"model","code_conditions":manual})

    def gate(self,rule,snapshot,now):
        results=[];unknown=False;failed=False
        for c in rule["code_conditions"]:
            if c["mode"]=="off":continue
            kind=c["kind"];values=c["values"];actual=None
            if kind=="source_in":actual=snapshot["kind"] in values
            if kind=="author_in":
                native=terminal("conditions",conditions=[{"kind":"author_in","authors":values,"mode":"require","origin":"manual"}],source=snapshot)
                actual=native["results"][0]
            if kind=="explicit_asset":actual=bool(snapshot["asset_uids"])
            if kind=="asset_in_watchlist":actual=snapshot.get("watchlist_at_source")
            if kind=="catalyst_kind":actual=snapshot.get("catalyst_kind") in values if snapshot.get("catalyst_kind") else None
            if kind=="catalyst_stage":actual=snapshot.get("catalyst_stage") in values if snapshot.get("catalyst_stage") else None
            if kind=="min_material_freshness":actual=0<=now-snapshot["at"]<=float(values)*3600000
            if kind=="has_verified_quote":actual=snapshot.get("verified_quote")
            results.append({"condition":c,"result":actual})
            if c["mode"]=="require":unknown|=actual is None;failed|=actual is False
        return ("abstain" if unknown else "no_match" if failed else "pass"),results

    def decide(self,rule,snapshot,examples,now):
        decision,conditions=self.gate(rule,snapshot,now)
        result={"decision":decision,"rationale":"Условия кода", "condition_results":conditions,"retrieval_method":rule["retrieval_method"],
                "pipeline_version":"playbook-native-1.0","usage":None,"input_hash":digest([rule["revision_id"],snapshot,examples])}
        if decision!="pass":return result
        examples=sorted(examples,key=lambda e:(e["available_at"],e["revision_id"]),reverse=True)[:200]
        positives=[e for e in examples if e["label"]=="positive"];negatives=[e for e in examples if e["label"]=="negative"]
        if not positives:return {**result,"decision":"abstain","rationale":"Нет размеченных обучающих примеров"}
        result["retrieval_limits"]={"training_examples":200,"text_chars":4000,"summary_chars":1500,"selected_examples":len(examples)}
        ranked=terminal("lexical",query=retrieval_snapshot(snapshot),items=[{"item":e["revision_id"],"snap":retrieval_snapshot(e["source_snapshot"])} for e in positives],extra_docs=[rule["text"]])
        score=ranked[0]["score"] if ranked else 0
        if score<rule["retrieval_threshold"]:return {**result,"decision":"no_match","rationale":"Не прошёл текстовый отбор","retrieval_score":score,"retrieval_miss":True}
        if rule["retrieval_method"]=="lexical":return {**result,"decision":"match","rationale":"Текстовое сходство с размеченными примерами; смысловое соответствие требует проверки.","retrieval_score":score}
        post=retrieval_snapshot(snapshot);post.update(text=post["text"][:1500],summary=post["summary"][:800])
        response=self.model.call("match",{"post":post,"patterns":[{"id":rule["id"],"rule":{"summary_ru":rule["text"],"signs_ru":rule["positive_signs"],"anti_signs_ru":rule["negative_signs"]},
            "positive_examples":[model_example(e) for e in positives if e["revision_id"] in {r["item"] for r in ranked[:2]}],"negative_examples":[model_example(e) for e in negatives[:1]]}]})
        matches=response["output"].get("matches")
        if not isinstance(matches,list) or len(matches)!=1 or matches[0].get("pattern_id")!=rule["id"] or not isinstance(matches[0].get("match"),bool):
            raise ResearchError("invalid_model_json","Ответ модели не соответствует проверяемому правилу",502)
        m=matches[0]
        if m.get("symbol") and m["symbol"] not in snapshot["tickers"]:raise ResearchError("unverified_identity","Модель добавила отсутствующий актив",502)
        return {**result,"decision":"match" if m["match"] else "no_match","rationale":text(m.get("why_ru"),"why_ru",1,2000),"usage":response["usage"],"model":response["usage"]["model"],"raw_response":m}

    def evaluate(self,identifier,dataset_id,partition="holdout",should_stop=lambda:False,max_new_attempts=None,retry_key=None):
        rule=self.r.get("rule",identifier);data=self.r.dataset(dataset_id,"playbook")
        if data["section_id"]!=rule["section_id"]:raise ResearchError("dataset_mismatch","Другая рубрика")
        choice(partition,{"validation","holdout"},"partition")
        selected=[e for e in data["examples"] if e["revision_id"] in data["partitions"][partition]]
        train=[e for e in data["examples"] if e["revision_id"] in data["partitions"]["train"]]
        seen=set(rule["evidence_example_revision_ids"]);held={e["revision_id"] for e in selected}
        if seen&held:raise ResearchError("holdout_leakage","Пример проверки использован при построении правила",422)
        if seen-set(data["partitions"]["train"]):raise ResearchError("training_lineage","Происхождение правила не ограничено train этого снимка",422)
        if rule["retrieval_method"]=="model" and len(selected)>150:raise ResearchError("call_limit","Не более 150 модельных проверок за задание")
        if len(selected)>500:raise ResearchError("evaluation_limit","Не более 500 проверочных примеров за один запуск")
        counts={k:0 for k in ("tp","fp","fn","tn","abstained","failed","skipped","attempted","evaluated","positive","negative")};details=[]
        new_attempts=0;unfinished=False
        for example in selected:
            if should_stop():break
            if example["label"] not in {"positive","negative"}:counts["skipped"]+=1;continue
            counts[example["label"]]+=1;counts["attempted"]+=1
            attempt_key=digest([rule["revision_id"],dataset_id,partition,example["revision_id"]])
            cached=self.s.one("SELECT payload FROM research_note WHERE object_id=? AND kind='evaluation_attempt' AND dedup_key=?",(identifier,attempt_key))
            if cached and decode(cached["payload"]).get("decision")=="error" and retry_key:
                attempt_key=digest([attempt_key,text(retry_key,"retry_key",1,100)])
                cached=self.s.one("SELECT payload FROM research_note WHERE object_id=? AND kind='evaluation_attempt' AND dedup_key=?",(identifier,attempt_key))
            if cached:decision=decode(cached["payload"])
            else:
                if max_new_attempts is not None and new_attempts>=max_new_attempts:
                    counts[example["label"]]-=1;counts["attempted"]-=1;unfinished=True;break
                try:decision=self.decide(rule,example["source_snapshot"],train,data["cutoff_at"])
                except ResearchError as error:decision={"decision":"error","error_code":error.code}
                self.r.note(identifier,"evaluation_attempt",decision,rule["revision_id"],attempt_key)
                new_attempts+=1
            if decision["decision"]=="error":counts["failed"]+=1
            d=decision["decision"];positive=example["label"]=="positive"
            if d=="abstain":counts["abstained"]+=1;counts["fn"]+=int(positive)
            elif d in {"match","no_match"}:
                counts["evaluated"]+=1;counts["tp" if positive and d=="match" else "fp" if d=="match" else "fn" if positive else "tn"]+=1
            details.append({"example_revision_id":example["revision_id"],"label":example["label"],**decision})
        precision=counts["tp"]/(counts["tp"]+counts["fp"]) if counts["tp"]+counts["fp"] else None
        recall=counts["tp"]/counts["positive"] if counts["positive"] else None
        independent=not data["development_only"] and (rule["provenance"]=="model" or rule.get("independence_declaration"))
        sufficient=counts["positive"]>=30 and counts["negative"]>=30
        denominator=counts["tp"]+counts["fp"];interval=None
        if denominator:
            z=1.96;p=counts["tp"]/denominator;scale=1+z*z/denominator
            middle=(p+z*z/(2*denominator))/scale;radius=z*math.sqrt(p*(1-p)/denominator+z*z/(4*denominator*denominator))/scale
            interval=[middle-radius,middle+radius]
        payload={"rule_id":identifier,"rule_revision_id":rule["revision_id"],"dataset_id":dataset_id,"partition":partition,
            "counts":counts,"precision":precision,"recall":recall,"coverage":counts["evaluated"]/counts["attempted"] if counts["attempted"] else None,
            "status":"cancelled" if should_stop() else "running" if unfinished else "incomplete" if counts["failed"] else "completed","independent":independent,"sufficient_sample":sufficient,
            "quality":"independent" if independent and sufficient else "development_only","precision_interval_95":interval,"details":details,"pipeline_version":"playbook-native-1.0"}
        report=self.r.snapshot("playbook_evaluation",payload)
        self.r.note(identifier,"evaluation",{"evaluation_id":report["id"],"rule_revision_id":rule["revision_id"]},rule["revision_id"])
        return report

    def transition(self,identifier,body):
        rule=self.r.get("rule",identifier);target=choice(body.get("status"),{"shadow","active","retired"},"status")
        if target=="active":
            report=self.r.dataset(body.get("evaluation_id"),"playbook_evaluation")
            if report["rule_revision_id"]!=rule["revision_id"] or not report["independent"] or not report["sufficient_sample"] or report["status"]!="completed" or (report["precision"] or 0)<.8 or (report["recall"] or 0)<.6:
                raise ResearchError("evaluation_gate","Для active нужна независимая завершённая оценка ≥30/30, precision ≥0,80 и recall ≥0,60",422)
            self.r.note(identifier,"activation_evidence",{"evaluation_id":report["id"]},rule["revision_id"])
        return self.r.save("rule",{**rule,"status":target,"matching_revision_id":rule.get("matching_revision_id") or rule["revision_id"]},identifier,body.get("expected_revision"),body.get("reason"))

    def backfill(self,identifier,from_at,to_at,mode="backfill",should_stop=lambda:False):
        rule=self.r.get("rule",identifier)
        if rule["status"] not in {"shadow","active"}:raise ResearchError("rule_inactive","Переведите правило в теневой режим или active")
        if not isinstance(from_at,int) or not isinstance(to_at,int) or not 0<to_at-from_at<=7*DAY:raise ResearchError("range_limit","Не более 7 дней за запуск")
        data=self.r.dataset(rule["dataset_id"],"playbook") if rule.get("dataset_id") else None
        if not data:raise ResearchError("dataset_required","Зафиксируйте train dataset для поиска")
        if data["section_id"]!=rule["section_id"]:raise ResearchError("dataset_mismatch","Другая рубрика")
        train=[e for e in data["examples"] if e["revision_id"] in data["partitions"]["train"]]
        identity=rule.get("matching_revision_id") or rule["revision_id"]
        state_key="backfill:"+digest([identity,from_at,to_at,mode]);state=self.s.state(state_key)
        end=state.get("cutoff_at",self.s.clock());last_at=state.get("last_at",0);last_id=state.get("last_id","")
        calls=state.get("calls",0);processed=0
        chunk_limit=5 if rule["retrieval_method"]=="model" else 50
        while processed<chunk_limit and not should_stop():
            rows=self.s.rows("SELECT r.* FROM research_material_revision r WHERE r.published_at>=? AND r.published_at<? AND r.available_at<=? "
                "AND (r.available_at>? OR (r.available_at=? AND r.revision_id>?)) AND r.lifecycle='active' ORDER BY r.available_at,r.revision_id LIMIT 20",
                (from_at,to_at,end,last_at,last_at,last_id))
            if not rows:break
            for row in rows:
                if should_stop():break
                if rule["retrieval_method"]=="model" and calls>=150:raise ResearchError("call_limit","Достигнут предел модельных проверок backfill")
                ref={"kind":"material","id":row["material_id"],"revision_id":row["revision_id"]}
                key=digest([identity,row["revision_id"],mode]);already=self.s.one("SELECT id FROM research_object WHERE id=?",(key,))
                if not already:
                    try:decision=self.decide(rule,self.source_snapshot(ref,end),train,end)
                    except ResearchError as error:decision={"decision":"error","error_code":error.code,"rationale":error.message}
                    calls+=int(rule["retrieval_method"]=="model")
                    value={"rule_id":identifier,"rule_revision_id":identity,"source_ref":ref,"source_revision_id":row["revision_id"],"mode":mode,"status":"new",
                        "title":decode(row["payload"]).get("title","Материал"),"evaluated_at":self.s.clock(),"snapshot_as_of":end,**decision}
                    # Deterministic object ID makes restarts idempotent, including recorded errors.
                    with self.s.db.batch_writes():self.r.save("match",value,reason="Проверен материал правила",actor="worker",new_id=key)
                last_at=row["available_at"];last_id=row["revision_id"];processed+=1
                self.s.set_state(state_key,{"last_at":last_at,"last_id":last_id,"cutoff_at":end,"calls":calls})
                if processed>=chunk_limit:break
            if len(rows)<20:break
        remaining=self.s.one("SELECT COUNT(*) AS n FROM research_material_revision WHERE lifecycle='active' AND published_at>=? AND published_at<? AND available_at<=? AND (available_at>? OR (available_at=? AND revision_id>?))",
            (from_at,to_at,end,last_at,last_at,last_id))["n"]
        more=remaining>0 and (rule["retrieval_method"]!="model" or calls<150) and not should_stop()
        return {"processed":processed,"calls":calls,"cutoff_at":end,"cursor":{"at":last_at,"id":last_id},"interrupted":should_stop(),"more_work":more,
                "coverage":"partial_call_limit" if remaining and not more else "complete" if not remaining else "pending","remaining":remaining}

    def feedback(self,identifier,body):
        match=self.r.get("match",identifier)
        return self.r.note(identifier,"feedback",{"verdict":choice(body.get("verdict"),{"correct","incorrect","uncertain"},"verdict"),
            "explanation":text(body.get("explanation"),"explanation",5,4000),"reviewed_revision_id":match["revision_id"]},match["revision_id"],body.get("request_key"))

    def retry(self,identifier,should_stop=lambda:False):
        match=self.r.get("match",identifier)
        if match["decision"]!="error":return match
        if should_stop():return match
        rule=self.r.get("rule",match["rule_id"],match["rule_revision_id"])
        data=self.r.dataset(rule["dataset_id"],"playbook")
        train=[e for e in data["examples"] if e["revision_id"] in data["partitions"]["train"]]
        moment=match.get("snapshot_as_of",match["evaluated_at"])
        try:decision=self.decide(rule,self.source_snapshot(match["source_ref"],moment),train,moment)
        except ResearchError as error:decision={"decision":"error","error_code":error.code,"rationale":error.message}
        value={k:v for k,v in match.items() if k not in {"error_code","raw_response","usage","model"}}
        return self.r.save("match",{**value,**decision,"retried_at":self.s.clock()},identifier,match["revision_id"],"Явный повтор проверки после ошибки",actor="worker")

    def memory(self,section_id,dataset_id,query,max_tokens=2048):
        data=self.r.dataset(dataset_id,"playbook")
        if data["section_id"]!=section_id:raise ResearchError("dataset_mismatch","Другая рубрика")
        examples=[e for e in data["examples"] if e["revision_id"] in data["partitions"]["train"]]
        examples=sorted(examples,key=lambda e:(e["available_at"],e["revision_id"]),reverse=True)[:200]
        by_revision={e["revision_id"]:e for e in examples}
        ranked=terminal("lexical",query=retrieval_snapshot(query),items=[{"item":e["revision_id"],"snap":retrieval_snapshot(e["source_snapshot"])} for e in examples])
        result=[];used=0
        for row in ranked[:5]:
            e=by_revision[row["item"]];entry={"revision_id":e["revision_id"],"role":"historical_example","available_at":e["available_at"],"label":e["label"],"explanation":e["explanation"]}
            size=len(encode(entry).encode("utf-8"))
            if used+size>max_tokens:break
            used+=size;result.append(entry)
        return {"examples":result,"retrieval_method":"native lexical TF-IDF","token_budget":max_tokens,"token_upper_bound":used,"token_count_method":"conservative UTF-8 byte upper bound","truncated":len(result)<min(5,len(ranked))}
