"""Preparation, versioning and isolated execution of reproducible lab studies."""
import csv
from copy import deepcopy
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from .contracts import ResearchError,choice,text,timestamp,uid,encode,decode,digest
from .records import Records
from .market import Market,DAY,HOUR
from .terminal_bridge import execute as terminal

ENTRY={"relative_strength","breakout","dip_buy"}
EXIT={"take_profit","stop_loss","time_stop","trailing","rs_fade"}


class Lab:
    def __init__(self,store):self.s=store;self.r=Records(store);self.market=Market(store)

    @staticmethod
    def code_fingerprints():
        root=Path(__file__).parents[1]
        files=[Path(__file__).with_name(name) for name in ("lab_compute.py","lab_worker.py","terminal-runtime.cjs")]
        files+=[root/"config.py",*sorted((root/"engine").glob("*.py"))]
        return {str(path.relative_to(root)).replace('\\','/'):digest(path.read_text(encoding="utf-8")) for path in files}

    def spec(self,body):
        start=timestamp(body.get("from_at"),"from_at",optional=False);end=timestamp(body.get("to_at"),"to_at",optional=False)
        if not 0<end-start<=366*DAY:raise ResearchError("range_limit","Период должен быть от часа до 366 дней")
        identifiers=body.get("instrument_ids",[])
        if not isinstance(identifiers,list) or not 1<=len(identifiers)<=20:raise ResearchError("scope_limit","От 1 до 20 spot-инструментов")
        for identifier in identifiers:
            if not isinstance(identifier,int) or isinstance(identifier,bool):raise ResearchError("invalid_instrument","Лаборатория использует локальную H1-историю инструмента")
            self.market.instrument(identifier)
        unsupported=set(body.get("factors",[]))-{"price","catalyst_stage","in_htf_zone"}
        if unsupported:raise ResearchError("unsupported_factor","Нет исторического провайдера: "+", ".join(sorted(unsupported)),422)
        universe=body.get("universe_type","manual_list")
        if universe!="manual_list":raise ResearchError("unsupported_universe","Исторический top-N и текущие рейтинги нельзя подменять ручной вселенной",422)
        horizons=body.get("horizons_days",[1,3,7])
        if not isinstance(horizons,list) or not horizons or any(h not in {1,3,7} for h in horizons):raise ResearchError("invalid_horizon","Горизонты 1/3/7 дней")
        benchmark=body.get("benchmark_instrument_id") or self.market.benchmark()
        if benchmark:self.market.instrument(benchmark)
        technical=choice(body.get("technical_filter","none"),{"none","in_htf_zone"},"technical_filter")
        return {"title":text(body.get("title"),"title",5,200),"question":text(body.get("question"),"question",10,4000),
            "hypothesis":text(body.get("hypothesis",""),"hypothesis",0,8000),"experiment_type":choice(body.get("experiment_type","descriptive"),{"descriptive","simulation"},"experiment_type"),
            "instrument_ids":list(dict.fromkeys(identifiers)),"universe_type":"manual_list","universe_limitation":"selected_current_universe",
            "from_at":start,"to_at":end,"data_mode":choice(body.get("data_mode","observed_local"),{"observed_local","external_archive_reconstruction","reconstructed_current_rules"},"data_mode"),
            "catalyst_kind":body.get("catalyst_kind"),"catalyst_stage":choice(body.get("catalyst_stage","announced"),{"rumor","proposal","announced","live","denied","cancelled"},"catalyst_stage"),
            "episode_policy":choice(body.get("episode_policy","independent"),{"independent","overlapping"},"episode_policy"),"horizons_days":sorted(set(horizons)),
            "technical_filter":technical,"factors":body.get("factors",[]),"benchmark_instrument_id":benchmark,"method_version":"research-lab-1.0","status":"draft"}

    def experiment(self,body,identifier=None):
        prior=self.r.get("experiment",identifier) if identifier else {}
        return self.r.save("experiment",self.spec({**prior,**body}),identifier,body.get("expected_revision"),body.get("reason","Сохранён исследовательский вопрос"))

    def strategy(self,body,identifier=None):
        prior=self.r.get("strategy",identifier) if identifier else {};value={**prior,**body};config=value.get("config",{})
        for group,allowed in (("entry",ENTRY),("exit",EXIT)):
            rules=config.get(group,{}).get("rules",[])
            if not rules or len(rules)>10:raise ResearchError("rule_limit","От 1 до 10 правил в группе")
            for rule in rules:
                if rule.get("template") not in allowed:raise ResearchError("unsupported_factor","Правило не имеет подключённого проверяемого фактора: "+str(rule.get("template")),422)
                for key,v in rule.get("params",{}).items():
                    if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=10000:raise ResearchError("invalid_parameter","Параметры правил — конечные числа в допустимом диапазоне")
                    if any(p in key for p in ("days","bars","period")) and (v<1 or int(v)!=v or v>365):raise ResearchError("invalid_parameter","Периоды задаются целыми числами от 1 до 365")
        native=terminal("strategy_validate",config=config)
        if native["universe"]["type"]!="manual_list":raise ResearchError("unsupported_universe","Нужен явный фиксированный состав")
        if any(native["costs"][key]==0 for key in ("fee_bps","slippage_bps")) and value.get("idealized_costs") is not True:
            raise ResearchError("costs_required","Нулевые издержки требуют явной пометки idealized_costs")
        if native["exit"]["op"]!="OR":raise ResearchError("execution_policy","Защитные выходы первой версии объединяются OR",422)
        capital=float(value.get("initial_capital",10000))
        if not math.isfinite(capital) or not 1<=capital<=1e10:raise ResearchError("invalid_capital","Некорректный начальный капитал")
        return self.r.save("strategy",{"title":text(value.get("title"),"title",3,200),"config":native,"initial_capital":capital,"idealized_costs":bool(value.get("idealized_costs")),
            "order_type":choice(value.get("order_type","market"),{"market","limit"},"order_type"),"limit_offset_pct":max(0,min(20,float(value.get("limit_offset_pct",0)))),
            "limit_expiry_hours":max(1,min(168,int(value.get("limit_expiry_hours",24)))),"max_mark_age_hours":max(1,min(24,int(value.get("max_mark_age_hours",2)))),
            "end_policy":choice(value.get("end_policy","period_end"),{"period_end","marked_open"},"end_policy"),"status":"versioned","execution_policy":"next_h1_open_stop_first_v1",
            "rounding_policy":"idealized_size_rules","method_version":"lab-spot-h1-1.0"},identifier,body.get("expected_revision"),body.get("reason","Сохранена формальная стратегия"))

    def prepare(self,experiment_id,revision_id=None):
        spec=self.r.get("experiment",experiment_id,revision_id);cutoff=self.s.clock();identifiers=list(spec["instrument_ids"])
        if spec["benchmark_instrument_id"] not in identifiers and spec["benchmark_instrument_id"]:identifiers.append(spec["benchmark_instrument_id"])
        h1={};d1={};coverage={};count=0;resolvers={}
        with self.s.db.read_tx():
            for identifier in identifiers:
                ins=self.market.instrument(identifier);resolvers[str(identifier)]={"instrument":ins,"asset_link":self.s.instrument_link(identifier)}
                rows=self.market.bars(identifier,"H1",from_at=spec["from_at"],to_at=min(cutoff,spec["to_at"]+7*DAY+HOUR),limit=10001)
                daily=self.market.bars(identifier,"D1",from_at=spec["from_at"]-366*DAY,to_at=min(cutoff,spec["to_at"]),limit=1100)
                count+=len(rows)+len(daily)
                if len(rows)>10000 or count>250000:raise ResearchError("row_limit","Снимок превышает лимит 250 000 баров / 10 000 H1 на инструмент")
                h1[str(identifier)]=rows;d1[str(identifier)]=daily
                expected=max(0,(min(spec["to_at"],cutoff)//HOUR-(spec["from_at"]+HOUR-1)//HOUR))
                coverage[str(identifier)]={"h1_bars":len(rows),"d1_bars":len(daily),"expected_h1":expected,"invalid_h1":sum(not b["valid"] for b in rows),
                    "missing_h1":max(0,expected-len([b for b in rows if b["open_time"]<spec["to_at"]])),"source":ins["venue"]}
            revisions=self.s.rows("SELECT * FROM research_catalyst_revision WHERE available_at>=? AND available_at<? AND available_at<=? ORDER BY available_at,revision_id LIMIT 10001",
                (spec["from_at"],spec["to_at"],cutoff))
            if len(revisions)>10000:raise ResearchError("episode_limit","Не более 10 000 версий катализаторов за запуск")
            episodes=[];events=[]
            for row in revisions:
                value=decode(row["payload"]);events.append({**value,"available_at":row["available_at"]})
                if value["review_status"]!="accepted" or value["stage"]!=spec["catalyst_stage"] or (spec.get("catalyst_kind") and value["kind"]!=spec["catalyst_kind"]):continue
                for instrument in spec["instrument_ids"]:
                    link=self.s.instrument_link(instrument,row["available_at"])
                    if not link or (link.get("asset_uid") not in value.get("asset_uids",[]) and value.get("scope")!="global"):continue
                    alias=self.s.one("SELECT canonical_id FROM research_catalyst_alias WHERE alias_id=? AND available_at<=?",(row["catalyst_id"],row["available_at"]))
                    canonical=alias["canonical_id"] if alias else row["catalyst_id"]
                    episodes.append({"id":digest([row["revision_id"],instrument]),"canonical_catalyst_id":canonical,"trigger_ref":{"kind":"catalyst","id":row["catalyst_id"],"revision_id":row["revision_id"]},
                        "asset_uid":link.get("asset_uid"),"instrument_id":instrument,"stage":value["stage"],"kind":value["kind"],"decision_at":row["available_at"],"detected_at":row["available_at"],
                        "occurred_at":value.get("starts_at"),"available_at":row["available_at"],"cohort":"all"})
        limitations=["selected_current_universe","Historical market candle receipt times are unavailable; candle histories are frozen reconstructions.",
            "Informational factors use local available_at; external archive stage availability is not invented."]
        mode="mixed_reconstruction" if spec["data_mode"]=="observed_local" else spec["data_mode"]
        if spec["data_mode"]=="external_archive_reconstruction":
            raise ResearchError("external_availability_missing","Нужен архив с доказанными временами доступности событий, обычной даты публикации недостаточно",422)
        if spec["technical_filter"]!="none" and len(episodes)>50:raise ResearchError("replay_scope_limit","HTF replay первой версии ограничен 50 эпизодами за запуск")
        from dataclasses import asdict
        from ..config import DetectorConfig
        code=self.code_fingerprints()
        payload={"spec":spec,"spec_revision_id":spec["revision_id"],"experiment_id":experiment_id,"h1":h1,"d1":d1,"episodes":episodes,"catalyst_revisions":events,
            "resolvers":resolvers,"coverage":coverage,"cutoff_at":cutoff,"data_mode":mode,"factor_modes":{"market":"reconstruction","catalysts":"observed_local",
                "technical":"reconstructed_current_rules" if spec["technical_filter"]!="none" else "disabled"},"limitations":limitations,"code_fingerprints":code,
            "manifest_version":"research-dataset-1.0","rows":count,"status":"ready","detector_config":asdict(DetectorConfig())}
        with self.s.db.batch_writes():return self.r.snapshot("lab",payload,cutoff)

    def create_run(self,body):
        dataset=self.r.dataset(body.get("dataset_id"),"lab");spec=dataset["spec"]
        strategy=None
        if spec["experiment_type"]=="simulation":
            strategy=self.r.get("strategy",body.get("strategy_id"),body.get("strategy_revision_id"))
        identifier=uid();now=self.s.clock()
        payload={"spec_revision_id":spec["revision_id"],"experiment_id":dataset["experiment_id"],"dataset_id":dataset["id"],"strategy":strategy,"seed":int(body.get("seed",0)),
            "engine_version":"research-lab-1.0","status":"ready","progress":0,"code_fingerprints":dataset["code_fingerprints"]}
        self.s.execute("INSERT INTO research_result VALUES(?, 'lab_run', ?, ?, 'ready', ?, ?, NULL, ?)",(identifier,dataset["experiment_id"],dataset["id"],now,now,encode(payload)))
        return {"id":identifier,**payload}

    def run(self,identifier,should_stop=lambda:False):
        row=self.s.one("SELECT * FROM research_result WHERE id=? AND kind='lab_run'",(identifier,))
        if not row:raise ResearchError("run_not_found","Запуск не найден",404)
        if row["status"] in {"succeeded","incomplete","cancelled"}:return self.result(identifier)
        metadata=decode(row["payload"]);dataset=self.r.dataset(row["dataset_id"],"lab")
        if should_stop():return self.result(identifier)
        current_code=self.code_fingerprints()
        if current_code!=dataset["code_fingerprints"]:raise ResearchError("code_revision_changed","Код лаборатории изменился после подготовки снимка; создайте новый dataset",409)
        self.s.execute("UPDATE research_result SET status='running',updated_at=? WHERE id=?",(self.s.clock(),identifier))
        payload={"dataset":{k:v for k,v in dataset.items() if k not in {"id","content_hash"}},"dataset_hash":dataset["content_hash"],"spec":dataset["spec"],"strategy":metadata["strategy"]}
        process=subprocess.Popen([sys.executable,"-m","app.research.lab_worker"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding="utf-8",creationflags=0x08000000 if os.name=="nt" else 0)
        started=time.monotonic();failure=None
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(process.communicate,encode(payload))
            while True:
                try:stdout,stderr=pending.result(timeout=.2);break
                except TimeoutError:
                    cancelled=self.s.one("SELECT status FROM research_result WHERE id=?",(identifier,))["status"]=="cancelled"
                    if cancelled:failure="cancelled"
                    elif should_stop():failure="interrupted"
                    elif time.monotonic()-started>60:failure="timeout"
                    elif self.memory_bytes(process)>512*1024*1024:failure="memory_limit"
                    if failure:process.kill();stdout,stderr=pending.result(timeout=5);break
        if failure:
            if failure=="cancelled":return self.cancel(identifier)
            if failure=="interrupted":
                self.s.execute("UPDATE research_result SET status='ready',updated_at=? WHERE id=? AND status='running'",(self.s.clock(),identifier))
                return self.result(identifier)
            self.fail(identifier,failure);return self.result(identifier)
        if process.returncode!=0:
            self.fail(identifier,"worker_failed");return self.result(identifier)
        try:result=json.loads(stdout)
        except ValueError:self.fail(identifier,"invalid_worker_output");return self.result(identifier)
        expected=digest({k:v for k,v in result.items() if k!="result_hash"})
        if result.get("result_hash")!=expected:self.fail(identifier,"result_hash_mismatch");return self.result(identifier)
        with self.s.db.batch_writes():
            state=self.s.one("SELECT status FROM research_result WHERE id=?",(identifier,))["status"]
            if state=="cancelled":return self.result(identifier)
            self.s.execute("UPDATE research_result SET status=?,updated_at=?,content_hash=?,payload=? WHERE id=?",
                (result["status"],self.s.clock(),result["result_hash"],encode({**metadata,"status":result["status"],"progress":100,"result":result,"runtime_seconds":round(time.monotonic()-started,3)}),identifier))
        return self.result(identifier)

    @staticmethod
    def memory_bytes(process):
        if os.name!="nt":return 0
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD),*[(name,ctypes.c_size_t) for name in
                ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage","PrivateUsage")]]
        counter=Counters();counter.cb=ctypes.sizeof(counter)
        ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(int(process._handle)),ctypes.byref(counter),counter.cb)
        return max(counter.WorkingSetSize,counter.PrivateUsage)

    def fail(self,identifier,code):
        row=self.s.one("SELECT payload FROM research_result WHERE id=?",(identifier,));value=decode(row["payload"])
        self.s.execute("UPDATE research_result SET status='failed',updated_at=?,payload=? WHERE id=?",(self.s.clock(),encode({**value,"status":"failed","error_code":code}),identifier))

    def result(self,identifier,detail=True):
        row=self.s.one("SELECT * FROM research_result WHERE id=? AND kind='lab_run'",(identifier,))
        if not row:raise ResearchError("run_not_found","Запуск не найден",404)
        value=decode(row["payload"])
        if not detail and "result" in value:value["result"]={k:v for k,v in value["result"].items() if k not in {"episodes","outcomes","orders","trades","equity"}}
        return {**value,"id":identifier,"status":row["status"],"created_at":row["created_at"],"updated_at":row["updated_at"]}

    def cancel(self,identifier):
        self.result(identifier)
        self.s.execute("UPDATE research_result SET status='cancelled',updated_at=? WHERE id=? AND status IN ('ready','running','failed')",(self.s.clock(),identifier))
        return self.result(identifier,False)

    def rows(self,identifier,kind="episodes",offset=0,limit=100):
        choice(kind,{"episodes","outcomes","trades","orders","equity"},"kind")
        value=self.result(identifier);items=value.get("result",{}).get(kind,[])
        return {"items":items[offset:offset+min(100,max(1,limit))],"total":len(items),"next_offset":offset+limit if offset+limit<len(items) else None}

    def export(self,identifier):
        from zipfile import ZipFile,ZIP_DEFLATED
        run=self.result(identifier);dataset=self.r.dataset(run["dataset_id"],"lab")
        archive=io.BytesIO()
        with ZipFile(archive,"w",ZIP_DEFLATED) as z:
            z.writestr("manifest.json",encode({"run":run,"dataset":dataset}))
            for name in ("episodes","outcomes","trades","orders","equity"):
                rows=run.get("result",{}).get(name,[])
                if not rows:continue
                fields=sorted({key for row in rows for key in row});stream=io.StringIO(newline="");writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
                for row in rows:
                    safe={}
                    for key,value in row.items():
                        if isinstance(value,(dict,list)):value=encode(value)
                        if isinstance(value,str) and value.lstrip().startswith(("=","+","-","@","\t","\r")):value="'"+value
                        safe[key]=value
                    writer.writerow(safe)
                z.writestr(name+".csv",stream.getvalue().encode("utf-8-sig"))
        return archive.getvalue()

    def create_sweep(self,body):
        dataset=self.r.dataset(body.get("dataset_id"),"lab");strategy=self.r.get("strategy",body.get("strategy_id"),body.get("strategy_revision_id"))
        variants=body.get("variants",[])
        if not isinstance(variants,list) or not 1<=len(variants)<=100:raise ResearchError("sweep_limit","От 1 до 100 конфигураций")
        spec=dataset["spec"];train_to=timestamp(body.get("train_to"),"train_to",optional=False);validation_to=timestamp(body.get("validation_to"),"validation_to",optional=False)
        purge=max(24*max(spec["horizons_days"]),strategy["config"]["risk"]["time_stop"])
        embargo=max(1,int(body.get("embargo_hours",24)))
        if not spec["from_at"]<train_to<train_to+(purge+embargo)*HOUR<validation_to<validation_to+(purge+embargo)*HOUR<spec["to_at"]:
            raise ResearchError("invalid_split","Для train/validation/holdout нужны непересекающиеся периоды с purge и embargo",422)
        configs=[]
        allowed={"risk.stop_loss_pct","risk.max_position_pct","risk.max_positions","risk.time_stop","risk.max_drawdown_kill_pct","risk.cooldown_days","costs.fee_bps","costs.slippage_bps"}
        for changes in variants:
            if not isinstance(changes,dict) or not changes or set(changes)-allowed:raise ResearchError("invalid_sweep_parameter","Параметр вне allowlist sweep")
            config=deepcopy(strategy["config"])
            for path,value in changes.items():
                group,key=path.split(".");config[group][key]=value
                if path=="risk.stop_loss_pct":
                    for rule in config["exit"]["rules"]:
                        if rule["template"]=="stop_loss":rule["params"]["stop_loss_pct"]=value
            parsed=terminal("strategy_validate",config=config)
            configs.append({"changes":changes,"strategy":{**strategy,"config":parsed},"train_run":None,"validation_run":None})
        identifier=uid();now=self.s.clock()
        payload={"dataset_id":dataset["id"],"strategy_revision_id":strategy["revision_id"],"variants":configs,"train_to":train_to,"validation_to":validation_to,
            "purge_hours":purge,"embargo_hours":embargo,"selected_variant":None,"holdout_run":None,"objective":"validation net return_pct",
            "status":"ready","selection_policy":"holdout excluded from parameter choice","attempts":len(variants)}
        self.s.execute("INSERT INTO research_result VALUES(?,'lab_sweep',?,?,'ready',?,?,NULL,?)",(identifier,dataset["experiment_id"],dataset["id"],now,now,encode(payload)))
        return {"id":identifier,**payload}

    def sweep(self,identifier):
        row=self.s.one("SELECT * FROM research_result WHERE kind='lab_sweep' AND id=?",(identifier,))
        if not row:raise ResearchError("sweep_not_found","Подбор параметров не найден",404)
        return {**decode(row["payload"]),"id":identifier,"status":row["status"]}

    def execute_sweep(self,identifier,should_stop=lambda:False):
        sweep=self.sweep(identifier);dataset=self.r.dataset(sweep["dataset_id"],"lab");spec=dataset["spec"]
        if sweep["status"] in {"succeeded","incomplete","cancelled"}:return {"sweep_id":identifier,"status":sweep["status"]}
        def store(status):
            self.s.execute("UPDATE research_result SET payload=?,status=?,updated_at=? WHERE id=?",(encode(sweep),status,self.s.clock(),identifier))
        def part(name,strategy,slot):
            if slot:return slot
            bounds={"train":(spec["from_at"],sweep["train_to"]),"validation":(sweep["train_to"]+(sweep["purge_hours"]+sweep["embargo_hours"])*HOUR,sweep["validation_to"]),
                "holdout":(sweep["validation_to"]+(sweep["purge_hours"]+sweep["embargo_hours"])*HOUR,spec["to_at"])}
            start,end=bounds[name];frozen={k:v for k,v in dataset.items() if k not in {"id","content_hash"}}
            frozen["spec"]={**spec,"from_at":start,"to_at":end};frozen["episodes"]=[e for e in dataset["episodes"] if start<=e["decision_at"]<end]
            frozen["partition"]={"name":name,"from_at":start,"to_at":end,"parent_dataset_id":dataset["id"]}
            snap=self.r.snapshot("lab",frozen,dataset["cutoff_at"]);run=self.create_run({"dataset_id":snap["id"],"strategy_id":strategy["id"],"strategy_revision_id":strategy["revision_id"]})
            row=self.s.one("SELECT payload FROM research_result WHERE id=?",(run["id"],));metadata=decode(row["payload"]);metadata["strategy"]=strategy
            self.s.execute("UPDATE research_result SET payload=? WHERE id=?",(encode(metadata),run["id"]))
            return run["id"]
        for index,variant in enumerate(sweep["variants"]):
            if should_stop():store("cancelled");return {"sweep_id":identifier,"status":"cancelled"}
            if variant.get("validation_result") is not None:continue
            for name in ("train","validation"):
                slot=name+"_run";variant[slot]=part(name,variant["strategy"],variant.get(slot));store("running")
                run=self.run(variant[slot],should_stop)
                variant[name+"_result"]={"run_id":run["id"],"status":run["status"],"metrics":run.get("result",{}).get("metrics",{})}
            sweep["progress"]=int((index+1)/len(sweep["variants"])*90);store("running")
            return {"sweep_id":identifier,"more_work":True,"progress":sweep["progress"]}
        candidates=[(i,v) for i,v in enumerate(sweep["variants"]) if v.get("validation_result",{}).get("status")=="succeeded"]
        if not candidates:sweep["error_code"]="no_complete_validation";store("incomplete");return {"sweep_id":identifier,"status":"incomplete"}
        index,selected=max(candidates,key=lambda pair:(pair[1]["validation_result"]["metrics"].get("return_pct",-1e100),-pair[0]))
        sweep["selected_variant"]=index
        sweep["holdout_run"]=part("holdout",selected["strategy"],sweep.get("holdout_run"));store("running")
        run=self.run(sweep["holdout_run"],should_stop);sweep["holdout_result"]={"run_id":run["id"],"status":run["status"],"metrics":run.get("result",{}).get("metrics",{})}
        sweep["progress"]=100;store("succeeded" if run["status"]=="succeeded" else "incomplete")
        return {"sweep_id":identifier,"status":self.sweep(identifier)["status"]}
