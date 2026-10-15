"""Closed UTC-day statistics with immutable source fingerprints."""
import math
import statistics
from .contracts import ResearchError,digest,uid,encode,decode,cursor,uncursor
from .market import Market,DAY
from .records import Records


def metrics(asset,benchmark):
    a={r["close_boundary"]:r for r in asset}; b={r["close_boundary"]:r for r in benchmark}
    end=max(b,default=None);result={}
    def window(n):
        if end is None:return None,"benchmark_missing"
        times=[end-i*DAY for i in reversed(range(n+1))]
        if any(t not in a or t not in b for t in times):return None,"missing_bars"
        if any(not a[t]["valid"] or not b[t]["valid"] for t in times):return None,"invalid_ohlc"
        return ([a[t]["close"] for t in times],[b[t]["close"] for t in times]),None
    def put(name,n,value,reason=None):
        if value is not None and not math.isfinite(value):value=None;reason="numerical_range"
        result[name]={"value":value,"window":n,"sample_n":n if reason is None else 0,"from_at":end-n*DAY if end else None,
                      "to_at":end,"quality":"ok" if reason is None else "unavailable","reason_codes":[reason] if reason else []}
    for n in (7,30,90):
        pair,reason=window(n)
        ar=br=None
        if pair:
            x,y=pair;ar=x[-1]/x[0]-1;br=y[-1]/y[0]-1
        put("return_"+str(n),n,ar*100 if ar is not None else None,reason)
        put("excess_"+str(n),n,(ar-br)*100 if ar is not None else None,reason)
        put("rs_ratio_"+str(n),n,((1+ar)/(1+br)-1)*100 if ar is not None else None,reason)
    pair,reason=window(60)
    corr=beta=None
    if pair:
        x,y=pair;rx=[x[i]/x[i-1]-1 for i in range(1,61)];ry=[y[i]/y[i-1]-1 for i in range(1,61)]
        vx=statistics.variance(rx);vy=statistics.variance(ry)
        if vx==0 or vy==0:reason="constant_series"
        else:corr=statistics.correlation(rx,ry);beta=statistics.covariance(rx,ry)/vy
    put("correlation_60",60,corr,reason);put("beta_60",60,beta,reason)
    pair,reason=window(20)
    put("volatility_20",20,statistics.stdev([pair[0][i]/pair[0][i-1]-1 for i in range(1,21)])*math.sqrt(365)*100 if pair else None,reason)
    pair,reason=window(29)
    put("drawdown_30",30,(pair[0][-1]/max(pair[0])-1)*100 if pair else None,reason)
    return result


class Strength:
    def __init__(self,store):self.s=store;self.r=Records(store);self.market=Market(store)

    def calculate(self,identifiers=None,benchmark=None):
        benchmark=benchmark or self.market.benchmark()
        if not benchmark:raise ResearchError("benchmark_missing","Нет расчётного BTC spot USDT",422)
        if identifiers is None:
            identifiers=[i.id for i in self.s.db.get_instruments() if i.market_type=="spot" and i.quote_asset=="USDT"]
            alt=self.s.rows("SELECT id FROM alt_instrument_source WHERE quote='USDT' ORDER BY id LIMIT ?",(max(0,200-len(identifiers)),))
            identifiers+= ["alt:"+str(row["id"]) for row in alt]
        if not isinstance(identifiers,list) or len(identifiers)>200:raise ResearchError("scope_limit","Не более 200 инструментов")
        benchmark_rows=self.market.bars(benchmark,limit=92)
        rows=[];fingerprints=[]
        for identifier in dict.fromkeys(identifiers):
            ins=self.market.instrument(identifier);bars=self.market.bars(identifier,limit=92)
            values=metrics(bars,benchmark_rows)
            fingerprint=self.market.revision_fingerprint(identifier,"D1",bars);fingerprints.append(fingerprint)
            link=self.s.instrument_link(identifier) if isinstance(identifier,int) else None
            rows.append({"instrument_id":identifier,"asset_uid":link.get("asset_uid") if link else None,"symbol":ins["symbol"],
                         "venue":ins["venue"],"quote_asset":"USDT","source_revision_hash":fingerprint,"metrics":values,
                         "quality":"ok" if values["excess_7"]["value"] is not None else "incomplete"})
        content={"benchmark_instrument_id":benchmark,"method_version":"rs-utc-1.0","days":"UTC","items":rows,
                 "source_revision_hash":digest([fingerprints,benchmark_rows]),"as_of":max([b["close_boundary"] for b in benchmark_rows],default=None)}
        snapshot=self.r.snapshot("strength",content)
        with self.s.db.batch_writes():self.s.set_state("strength",{"snapshot_id":snapshot["id"],"last_success_at":self.s.clock()})
        return snapshot

    def list(self,*,snapshot_id=None,instrument_ids=None,asset_uids=None,metric="excess_30",sort="desc",after=None,limit=100,filters=None):
        if instrument_ids and asset_uids:raise ResearchError("invalid_scope","Выберите активы или инструменты, не оба",400)
        if len(instrument_ids or asset_uids or [])>200:raise ResearchError("scope_limit","Не более 200 инструментов")
        allowed={*['return_'+str(n) for n in (7,30,90)],*['excess_'+str(n) for n in (7,30,90)],*['rs_ratio_'+str(n) for n in (7,30,90)],
                 "correlation_60","beta_60","volatility_20","drawdown_30"}
        if metric not in allowed or sort not in {"asc","desc"}:raise ResearchError("invalid_sort","Неизвестная метрика или сортировка")
        pos=uncursor(after);filters=filters or {};scope=digest([instrument_ids,asset_uids,metric,sort,filters])
        if pos and pos.get("scope")!=scope:raise ResearchError("cursor_scope","Изменились фильтры",400)
        identifier=(pos["snapshot"] if pos else snapshot_id) or self.s.state("strength").get("snapshot_id")
        if not identifier:return {"items":[],"total":0,"quality":"unavailable","reason":"not_calculated","next_cursor":None}
        snap=self.r.dataset(identifier,"strength");rows=[]
        for row in snap["items"]:
            if instrument_ids and row["instrument_id"] not in instrument_ids:continue
            if asset_uids and row.get("asset_uid") not in asset_uids:continue
            if filters.get("quality") and row["quality"]!=filters["quality"]:continue
            keep=True
            for key,value in filters.items():
                if key=="quality":continue
                name,direction=key.rsplit("_",1)
                if name not in allowed or direction not in {"min","max"}:raise ResearchError("invalid_filter","Неизвестный фильтр")
                actual=row["metrics"][name]["value"]
                if actual is None or (direction=="min" and actual<float(value)) or (direction=="max" and actual>float(value)):keep=False
            if keep:rows.append(row)
        rows.sort(key=lambda row:(row["metrics"][metric]["value"] is None,
            (row["metrics"][metric]["value"] or 0)*(1 if sort=="asc" else -1),str(row["instrument_id"])))
        offset=pos["offset"] if pos else 0;limit=min(200,max(1,int(limit)))
        return {"snapshot_id":identifier,"items":rows[offset:offset+limit],"total":len(rows),"as_of":snap["as_of"],"method_version":snap["method_version"],
                "next_cursor":cursor({"scope":scope,"snapshot":identifier,"offset":offset+limit}) if offset+limit<len(rows) else None}

    def correlations(self,identifiers):
        if not identifiers or len(identifiers)>30:raise ResearchError("scope_limit","От 1 до 30 инструментов")
        series={str(i):self.market.bars(i,limit=61) for i in identifiers};matrix=[]
        for left in identifiers:
            for right in identifiers:
                result=metrics(series[str(left)],series[str(right)])["correlation_60"]
                matrix.append({"left":left,"right":right,**result})
        return self.r.snapshot("correlations",{"instrument_ids":identifiers,"matrix":matrix,"method_version":"rs-utc-1.0","series":series})
