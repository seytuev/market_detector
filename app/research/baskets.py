"""Versioned model baskets using STVTerminal's original buy-and-hold index."""
from decimal import Decimal,InvalidOperation
from .contracts import ResearchError,text,choice,uid,digest,encode,decode
from .records import Records
from .market import Market,HOUR
from .terminal_bridge import execute as terminal
from .store import local_id


class Baskets:
    def __init__(self,store):self.s=store;self.r=Records(store);self.market=Market(store)

    def validate(self,body):
        members=body.get("members",[])
        if not isinstance(members,list) or len(members)>30:raise ResearchError("member_limit","Не более 30 активов в корзине")
        seen=set();validated=[];total=Decimal(0)
        for member in members:
            asset=member.get("asset_uid");self.s.require_asset(asset)
            if asset in seen:raise ResearchError("duplicate_asset","Актив повторяется в корзине")
            seen.add(asset)
            instrument=self.market.instrument(member.get("instrument_id"))
            if isinstance(instrument["id"],str):raise ResearchError("unsupported_market","Наблюдение корзин требует инструмента с H1-историей")
            link=self.s.instrument_link(instrument["id"])
            if not link or link.get("asset_uid")!=asset:raise ResearchError("asset_mismatch","Проверьте связь актива и расчётного инструмента",422)
            try:weight=Decimal(str(member.get("weight_pct")))
            except InvalidOperation:raise ResearchError("invalid_weight","Некорректный вес")
            if not weight.is_finite() or weight<=0 or weight.as_tuple().exponent < -2:raise ResearchError("invalid_weight","Вес должен быть положительным, не более двух знаков после запятой")
            total+=weight
            validated.append({"asset_uid":asset,"instrument_id":instrument["id"],"weight_pct":str(weight),"sector":text(member.get("sector",""),"sector",0,100)})
        if total>100:raise ResearchError("weight_sum","Сумма весов не может превышать 100%")
        benchmark=body.get("benchmark_instrument_id") or self.market.benchmark()
        if benchmark:self.market.instrument(benchmark)
        idea=body.get("linked_idea_id")
        if idea:self.r.get("idea",idea)
        return {"title":text(body.get("title"),"title",3,200),"thesis":text(body.get("thesis",""),"thesis",0,8000),
            "members":validated,"cash_pct":str(Decimal(100)-total),"cash_policy":"USDT constant unit, no interest or depeg model",
            "benchmark_instrument_id":benchmark,"linked_idea_id":idea,"source_refs":body.get("source_refs",[]),
            "source_snapshots":self.r.sources(body.get("source_refs",[])),"method_version":"basket-buy-hold-1.0","rebalance_policy":"none",
            "external_origin":body.get("external_origin"),"external_revision_hash":body.get("external_revision_hash")}

    def import_external(self,body):
        external=text(body.get("external_id"),"external_id",1,200);state=body.get("state")
        if not isinstance(state,dict) or not isinstance(state.get("assets"),list) or len(state["assets"])>30 or len(state.get("history",[]))>20000:
            raise ResearchError("invalid_import","Некорректный или слишком большой архив корзины")
        identifier=local_id("stvterminal","research-basket",external);checksum=digest(state)
        previous=self.s.one("SELECT payload FROM research_object WHERE id=? AND kind='basket'",(identifier,))
        prior=decode(previous["payload"]) if previous else None
        if prior and prior.get("external_revision_hash")==checksum:return prior
        if prior and prior["actor"]!="stvterminal":
            self.r.note(identifier,"external_conflict",{"external_snapshot":state,"external_revision_hash":checksum},key=checksum)
            return {"id":identifier,"conflict":True}
        native=terminal("basket_indexes",baskets=[state["assets"]])[0]
        title=body.get("title") or "Корзина STVTerminal · "+", ".join(a.get("ticker","") for a in state["assets"][:3])
        value=self.validate({"title":title[:200],"thesis":body.get("thesis","")})
        value.update({"status":"draft","external_origin":{"provider":"stvterminal","id":external,"state":state,"index_at_archive":native,
            "data_mode":"external_archive_reconstruction","requires_market_resolution":True},"external_revision_hash":checksum})
        return self.r.save("basket",value,identifier if prior else None,prior["revision_id"] if prior else None,"Импортирован внешний состав без локального старта",actor="stvterminal",new_id=identifier)

    def save(self,body,identifier=None):
        prior=self.r.get("basket",identifier) if identifier else None
        value=self.validate({**(prior or {}),**body});value["status"]=prior["status"] if prior else "draft"
        with self.s.db.batch_writes():
            result=self.r.save("basket",value,identifier,body.get("expected_revision"),body.get("reason","Создана модельная корзина"))
            changed=prior and any(value[k]!=prior[k] for k in ("members","benchmark_instrument_id"))
            if changed and prior["status"]=="observing":self.new_segment(result)
        return result

    def new_segment(self,basket):
        if not basket["members"]:raise ResearchError("empty_basket","Для наблюдения нужен хотя бы один актив")
        prior=self.segments(basket["id"])
        payload={"basket_revision_id":basket["revision_id"],"requested_at":self.s.clock(),"members":basket["members"],
            "benchmark_instrument_id":basket["benchmark_instrument_id"],"status":"pending_baseline",
            "previous_segment_id":next((s["id"] for s in prior if s["status"]=="active"),None),"baseline":None,"started_at":None}
        note=self.r.note(basket["id"],"basket_segment",payload,basket["revision_id"])
        self.s.set_state("basket-segment:"+note["id"],{**payload,"id":note["id"]})
        return {**payload,"id":note["id"]}

    def segments(self,identifier):
        self.r.get("basket",identifier)
        notes=self.r.notes(identifier,"basket_segment",limit=100)["items"]
        return [self.s.state("basket-segment:"+n["id"]) or n for n in notes]

    def start(self,identifier,body):
        basket=self.r.get("basket",identifier)
        if basket["status"] not in {"draft","closed","paused","observing"}:raise ResearchError("invalid_transition","Корзина недоступна для запуска")
        if any(s["status"]=="pending_baseline" for s in self.segments(identifier)):raise ResearchError("pending_baseline","Уже ожидается общий старт",409)
        if basket["status"]=="observing" and any(s["status"]=="active" and s["basket_revision_id"]==basket["revision_id"] for s in self.segments(identifier)):
            raise ResearchError("already_observing","Этот состав уже наблюдается",409)
        with self.s.db.batch_writes():
            saved=self.r.save("basket",{**basket,"status":"observing"},identifier,body.get("expected_revision"),body.get("reason","Начато наблюдение модельной корзины"))
            return {"basket":saved,"segment":self.new_segment(saved)}

    def transition(self,identifier,body):
        prior=self.r.get("basket",identifier);target=body.get("status")
        transitions={"draft":{"archived"},"observing":{"paused","closed","archived"},"paused":{"observing","closed","archived"},"closed":{"archived"},"archived":set()}
        if target not in transitions[prior["status"]]:raise ResearchError("invalid_transition","Переход не разрешён")
        with self.s.db.batch_writes():
            value=self.r.save("basket",{**prior,"status":target},identifier,body.get("expected_revision"),body.get("reason"))
            self.r.note(identifier,"basket_transition",{"from":prior["status"],"to":target,"reason":body.get("reason")},value["revision_id"])
            if target in {"closed","archived"}:
                for segment in self.segments(identifier):
                    if segment["status"] in {"active","pending_baseline"}:
                        self.s.set_state("basket-segment:"+segment["id"],{**segment,"status":"closed","closed_at":self.s.clock()})
        return value

    def native_assets(self,members,bases,prices,at=0):
        return [{"id":str(m["instrument_id"]),"ticker":self.s.require_asset(m["asset_uid"])["display_symbol"],"name":"",
                 "provider":self.market.instrument(m["instrument_id"])["venue"],"assetId":m["asset_uid"],"weight":float(m["weight_pct"]),
                 "base":bases.get(str(m["instrument_id"])),"price":prices.get(str(m["instrument_id"])),"target":None,"at":at,"sector":m["sector"]} for m in members]

    def observe(self,identifier):
        basket=self.r.get("basket",identifier)
        if basket["status"]!="observing":return {"status":basket["status"]}
        moment=self.s.clock();segments=self.segments(identifier)
        for segment in segments:
            if segment["status"]!="pending_baseline":continue
            first=((segment["requested_at"]+HOUR-1)//HOUR)*HOUR
            identifiers=[m["instrument_id"] for m in segment["members"]]
            if segment["benchmark_instrument_id"]:identifiers.append(segment["benchmark_instrument_id"])
            baseline=None
            for boundary in range(first,min(moment,segment["requested_at"]+2*HOUR)+1,HOUR):
                prices={str(i):self.market.price_at(i,boundary) for i in identifiers}
                if all(prices.values()):baseline={"at":boundary,"prices":prices};break
            if baseline:
                snap=self.r.snapshot("basket_baseline",baseline)
                segment={**segment,"status":"active","started_at":baseline["at"],"baseline":snap,"baseline_snapshot_id":snap["id"]}
                with self.s.db.batch_writes():
                    for old in segments:
                        if old["status"]=="active":self.s.set_state("basket-segment:"+old["id"],{**old,"status":"closed","closed_at":baseline["at"]})
                    self.s.set_state("basket-segment:"+segment["id"],segment)
            elif moment>segment["requested_at"]+2*HOUR:
                self.s.set_state("basket-segment:"+segment["id"],{**segment,"status":"baseline_failed","reason":"common_h1_missing","missing_instruments":identifiers})
        for segment in self.segments(identifier):
            if segment["status"]!="active":continue
            boundary=moment//HOUR*HOUR
            if boundary<segment["started_at"]:continue
            bases={key:bar["close"] for key,bar in segment["baseline"]["prices"].items()}
            input_prices={str(m["instrument_id"]):self.market.price_at(m["instrument_id"],boundary) for m in segment["members"]}
            prices={key:bar["close"] if bar else None for key,bar in input_prices.items()}
            native=terminal("basket_indexes",baskets=[self.native_assets(segment["members"],bases,prices,boundary)])[0]
            coverage=sum(float(m["weight_pct"]) for m in segment["members"] if prices[str(m["instrument_id"])] is not None)+100-sum(float(m["weight_pct"]) for m in segment["members"])
            benchmark=segment["benchmark_instrument_id"];b=self.market.price_at(benchmark,boundary) if benchmark else None
            b0=bases.get(str(benchmark));br=(b["close"]/b0-1)*100 if b and b0 else None
            payload={"segment_id":segment["id"],"observed_at":boundary,"input_prices":input_prices,"price_revision_hash":digest(input_prices),
                "index_value":native["value"],"return_pct":native["change"],"cash_pct":native["cash"],"contributions":native["assets"],
                "coverage_weight_pct":coverage,"quality":"ok" if native["value"] is not None else "incomplete",
                "reason_codes":[] if native["value"] is not None else ["missing_price"],"benchmark_return_pct":br,
                "excess_return_pct":native["change"]-br if native["change"] is not None and br is not None else None,"data_mode":"observed_local"}
            checksum=digest(payload)
            self.s.execute("INSERT INTO research_series VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,object_id,at_ms,content_hash) DO NOTHING",
                           (uid(),"basket_observation",segment["id"],boundary,moment,checksum,encode(payload)))
        return {"segments":self.segments(identifier)}

    def observations(self,identifier,segment_id=None,from_at=None,to_at=None,after=None,limit=100):
        from .contracts import cursor,uncursor
        segments=self.segments(identifier);ids={s["id"] for s in segments}
        segment_id=segment_id or (segments[0]["id"] if segments else None)
        if segment_id is None:return {"items":[],"next_cursor":None}
        if segment_id not in ids:raise ResearchError("invalid_segment","Сегмент не принадлежит корзине")
        pos=uncursor(after);scope=digest([identifier,segment_id,from_at,to_at]);args=[segment_id];where=["kind='basket_observation'","object_id=?"]
        if from_at is not None:where.append("at_ms>=?");args.append(from_at)
        if to_at is not None:where.append("at_ms<?");args.append(to_at)
        if pos:
            if pos.get("scope")!=scope:raise ResearchError("cursor_scope","Изменился период")
            where.append("(at_ms>? OR (at_ms=? AND id>?))");args.extend([pos["at"],pos["at"],pos["id"]])
        limit=min(100,max(1,int(limit)))
        rows=self.s.rows("SELECT * FROM research_series WHERE "+" AND ".join(where)+" ORDER BY at_ms,id LIMIT ?",(*args,limit+1))
        return {"items":[{**decode(r["payload"]),"id":r["id"],"available_at":r["available_at"]} for r in rows[:limit]],
            "next_cursor":cursor({"scope":scope,"at":rows[limit-1]["at_ms"],"id":rows[limit-1]["id"]}) if len(rows)>limit else None}

    def maintenance(self,limit=20):
        state=self.s.state("baskets-maintenance");after=state.get("after","")
        rows=self.s.rows("SELECT id FROM research_object WHERE kind='basket' AND status='observing' AND id>? ORDER BY id LIMIT ?",(after,limit))
        for row in rows:
            try:self.observe(row["id"])
            except ResearchError as error:self.s.set_state("basket-error:"+row["id"],{"error_code":error.code,"at":self.s.clock()})
        self.s.set_state("baskets-maintenance",{"after":rows[-1]["id"] if len(rows)==limit else ""})
