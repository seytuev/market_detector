"""Pure, isolated lab execution on frozen inputs; no production DB or network access."""
from copy import deepcopy
import math
import statistics
from .contracts import digest,ResearchError
from .market import HOUR,DAY
from .terminal_bridge import execute as terminal


def finite_result(value):
    if isinstance(value,float) and not math.isfinite(value):raise ValueError("non_finite_result")
    if isinstance(value,dict):
        for v in value.values():finite_result(v)
    if isinstance(value,list):
        for v in value:finite_result(v)
    return value


def descriptive(data,spec):
    horizons=spec.get("horizons_days",[1,3,7]);max_horizon=max(horizons)*DAY
    bars={key:{b["open_time"]:b for b in rows} for key,rows in data["h1"].items()}
    benchmark=bars.get(str(spec.get("benchmark_instrument_id")),{})
    previous={};seen=set();episodes=[];outcomes=[]
    for raw in sorted(data["episodes"],key=lambda e:(e["decision_at"],str(e["instrument_id"]),e["id"])):
        e=deepcopy(raw);key=(e["canonical_catalyst_id"],e["stage"],e["instrument_id"])
        e["exclusion_reason"]=None
        if key in seen:e["exclusion_reason"]="duplicate_event_stage"
        elif spec.get("episode_policy","independent")=="independent" and e["decision_at"]<previous.get(e["instrument_id"],0):e["exclusion_reason"]="overlap_cooldown"
        seen.add(key)
        if not e["exclusion_reason"]:previous[e["instrument_id"]]=e["decision_at"]+max_horizon
        if e.get("input_exclusion"):e["exclusion_reason"]=e["input_exclusion"]
        start=(e["decision_at"]//HOUR+1)*HOUR
        source=bars.get(str(e["instrument_id"]),{});base=source.get(start)
        e.update({"baseline_at":start,"baseline":base["open"] if base and base["valid"] else None,"method_version":"lab_next_h1_open_v1"})
        if not base or not base["valid"]:e["exclusion_reason"]=e["exclusion_reason"] or "baseline_missing"
        if spec.get("technical_filter")=="in_htf_zone" and not e["exclusion_reason"]:
            factor=e.get("technical_factor")
            if factor is None:e["exclusion_reason"]="technical_unavailable"
            else:e["cohort"]="in_zone" if factor else "outside_zone"
        episodes.append(e)
        if e["exclusion_reason"]:continue
        for days in horizons:
            target=start+days*DAY;actual=target;end=source.get(target-HOUR)
            if not end or not end["valid"]:actual=target+HOUR;end=source.get(target)
            good=end and end["valid"] and end["close_boundary"]<=data["cutoff_at"]
            path=[source.get(t) for t in range(start,actual,HOUR)]
            complete=all(b and b["valid"] for b in path)
            b0=benchmark.get(start);b1=benchmark.get(actual-HOUR)
            br=(b1["close"]/b0["open"]-1)*100 if good and b0 and b1 and b0["valid"] and b1["valid"] else None
            ret=(end["close"]/base["open"]-1)*100 if good else None
            outcomes.append({"episode_id":e["id"],"instrument_id":e["instrument_id"],"cohort":e.get("cohort","all"),"horizon_days":days,
                "baseline_at":start,"baseline":base["open"],"target_at":target,"actual_at":actual if good else None,"price":end["close"] if good else None,
                "return_pct":ret,"benchmark_return_pct":br,"excess_return_pct":ret-br if ret is not None and br is not None else None,
                "quality":"exact" if good and actual==target else "shifted" if good else "pending" if target>data["cutoff_at"] else "missing",
                "max_favourable_pct":(max(b["high"] for b in path)/base["open"]-1)*100 if complete and path else None,
                "max_adverse_pct":(min(b["low"] for b in path)/base["open"]-1)*100 if complete and path else None,"path_complete":complete})
    groups=[]
    def quantile(values,p):
        if not values:return None
        x=sorted(values);i=(len(x)-1)*p;l=int(i);return x[l]+(x[min(l+1,len(x)-1)]-x[l])*(i-l)
    for cohort in sorted({o["cohort"] for o in outcomes}):
        for days in horizons:
            rows=[o for o in outcomes if o["cohort"]==cohort and o["horizon_days"]==days];values=[o["return_pct"] for o in rows if o["return_pct"] is not None]
            excess=[o["excess_return_pct"] for o in rows if o["excess_return_pct"] is not None]
            groups.append({"cohort":cohort,"horizon_days":days,"n":len(values),"denominator":len(rows),"positive":sum(v>0 for v in values),"negative":sum(v<0 for v in values),
                "zero":sum(v==0 for v in values),"mean":statistics.mean(values) if values else None,"median":statistics.median(values) if values else None,
                "quantiles":{str(p):quantile(values,p) for p in (.1,.25,.75,.9)},"mean_excess":statistics.mean(excess) if excess else None,
                "exact":sum(o["quality"]=="exact" for o in rows),"shifted":sum(o["quality"]=="shifted" for o in rows),"missing":sum(o["quality"] in {"missing","pending"} for o in rows),
                "quality":"ok" if len(values)>=30 else "insufficient_sample"})
    return {"status":"incomplete" if any(o["quality"] in {"missing","pending"} for o in outcomes) else "succeeded","episodes":episodes,"outcomes":outcomes,"groups":groups,
        "metrics":{"triggers":len(episodes),"independent_episodes":sum(not e["exclusion_reason"] for e in episodes),"excluded":sum(bool(e["exclusion_reason"]) for e in episodes)},"method_version":"lab_next_h1_open_v1"}


def simulate(data,spec,strategy):
    config=strategy["config"];risk=config["risk"];costs=config["costs"]
    fee=costs["fee_bps"]/10000;slip=costs["slippage_bps"]/10000
    initial=float(strategy["initial_capital"]);cash=initial;peak=initial;positions={};pending={};cooldown={};trades=[];equity=[];orders=[];seq=0
    bars={key:{b["open_time"]:b for b in rows} for key,rows in data["h1"].items()};benchmark_id=str(spec.get("benchmark_instrument_id"))
    entry=config["entry"];exit_group=config["exit"]
    # Position-dependent exits are executed using actual intrabar OHLC below.
    price_exit={"op":exit_group["op"],"rules":[r for r in exit_group["rules"] if r["template"] in {"rs_fade","relative_strength","breakout","dip_buy"}]}
    signals=terminal("rule_signals",series={k:v for k,v in data["d1"].items() if k in [str(i) for i in spec["instrument_ids"]]},
        benchmark=data["d1"].get(benchmark_id,[]),entry=entry,exit=price_exit)
    signalmap={}
    for signal in signals:signalmap.setdefault(signal["signal_time"],[]).append(signal)
    latest={};last_entry_signal={};missing=[];kill=False;max_mark_age=strategy.get("max_mark_age_hours",2)*HOUR
    end=spec["to_at"]//HOUR*HOUR;start=((spec["from_at"]+HOUR-1)//HOUR)*HOUR
    def trace(kind,**fields):
        nonlocal seq
        seq+=1;orders.append({"sequence":seq,"kind":kind,**fields})
    def close(i,p,price,at,reason):
        nonlocal cash
        fill=price*(1-slip);gross=p["qty"]*fill;commission=gross*fee;cash+=gross-commission
        net=gross-commission-p["cost"]
        trades.append({"instrument_id":i,"signal_time":p["signal_time"],"entry_at":p["at"],"entry_price":p["price"],"qty":p["qty"],"entry_fee":p["fee"],
            "exit_at":at,"exit_price":fill,"exit_fee":commission,"net_pnl":net,"return_pct":net/p["cost"]*100,"exit_reason":reason,"hold_hours":(at-p["at"])/HOUR})
        trace("sell",instrument_id=i,at=at,price=fill,qty=p["qty"],fee=commission,reason=reason,cash=cash)
        del positions[i];cooldown[i]=at+risk["cooldown_days"]*DAY
    for at in range(start,end,HOUR):
        current={i:bars.get(i,{}).get(at) for i in [str(x) for x in spec["instrument_ids"]]}
        pre_stale=any(i not in latest or at-latest[i][1]>max_mark_age for i in positions)
        for i,order in sorted(list(pending.items()),key=lambda pair:(pair[1]["kind"]!="exit",int(pair[0]))):
            bar=current.get(i)
            if not bar or not bar["valid"]:
                if at>order["active_from"]+HOUR:trace("rejected",instrument_id=i,at=at,reason="entry_bar_missing");del pending[i]
                continue
            if order["kind"]=="exit":
                if i in positions:close(i,positions[i],bar["open"],at,"rule_exit")
                del pending[i];continue
            if i in positions or kill or pre_stale or len(positions)>=risk["max_positions"]:del pending[i];continue
            if strategy.get("order_type","market")=="limit":
                level=order["limit"]
                if not bar["low"]<level:
                    if at>=order["active_from"]+strategy.get("limit_expiry_hours",24)*HOUR:trace("expired",instrument_id=i,at=at);del pending[i]
                    continue
                price=min(bar["open"],level)
            else:price=bar["open"]
            mark_equity=cash+sum(p["qty"]*latest.get(k,(p["price"],p["at"]))[0] for k,p in positions.items())
            budget=min(cash,mark_equity*risk["max_position_pct"]/100)
            if budget<=0:trace("rejected",instrument_id=i,at=at,reason="insufficient_cash");del pending[i];continue
            fill=price*(1+slip);qty=budget/(fill*(1+fee));commission=qty*fill*fee;cash-=qty*fill+commission
            if cash< -1e-7:raise ValueError("negative_cash")
            cash=max(0,cash)
            stop=fill*(1-risk["stop_loss_pct"]/100);target=None
            for rule in exit_group["rules"]:
                if rule["template"]=="take_profit":target=fill*(1+float(rule["params"].get("take_profit_pct",10))/100)
                if rule["template"]=="stop_loss":stop=fill*(1-float(rule["params"].get("stop_loss_pct",risk["stop_loss_pct"]))/100)
            positions[i]={"at":at,"price":fill,"qty":qty,"fee":commission,"cost":budget,"signal_time":order["signal_time"],"stop":stop,"target":target,"peak":fill}
            trace("buy",instrument_id=i,signal_time=order["signal_time"],at=at,price=fill,qty=qty,fee=commission,cash=cash,rounding="idealized_size_rules")
            del pending[i]
        for i,p in list(positions.items()):
            bar=current.get(i)
            if not bar or not bar["valid"]:continue
            stop=p["stop"];target=p["target"]
            # Conservative same-bar ordering also applies after a limit fill.
            if bar["open"]<stop:close(i,p,bar["open"],at,"gap_stop");continue
            if bar["low"]<=stop:close(i,p,stop,at+HOUR,"stop");continue
            if target is not None and bar["high"]>=target and not (strategy.get("order_type")=="limit" and p["at"]==at):close(i,p,max(target,bar["open"]),at+HOUR,"target");continue
            p["peak"]=max(p["peak"],bar["high"])
            for rule in exit_group["rules"]:
                if rule["template"]=="trailing":
                    # New peak changes the next bar's stop, avoiding invented intrabar order.
                    p["stop"]=max(p["stop"],p["peak"]*(1-float(rule["params"].get("trail_pct",10))/100))
            max_hold=risk["time_stop"]
            for rule in exit_group["rules"]:
                if rule["template"]=="time_stop":max_hold=min(max_hold,int(rule["params"].get("bars",max_hold)))
            if (bar["close_boundary"]-p["at"])/HOUR>=max_hold:pending[i]={"kind":"exit","active_from":bar["close_boundary"]}
        for i,b in current.items():
            if b and b["valid"]:latest[i]=(b["close"],b["close_boundary"])
        value=cash;stale=False
        for i,p in positions.items():
            mark=latest.get(i)
            if mark is None or at+HOUR-mark[1]>max_mark_age:stale=True;missing.append({"instrument_id":i,"at":at+HOUR,"reason":"stale_mark"})
            value+=p["qty"]*(mark[0] if mark else p["price"])
        peak=max(peak,value);drawdown=(value/peak-1)*100
        kill=kill or drawdown<=-risk["max_drawdown_kill_pct"]
        equity.append({"at":at+HOUR,"cash":cash,"equity":value,"exposure":value-cash,"drawdown_pct":drawdown,"quality":"stale" if stale else "ok"})
        for signal in signalmap.get(at+HOUR,[]):
            i=signal["instrument_id"]
            if signal["exit"] and i in positions:pending[i]={"kind":"exit","active_from":at+HOUR}
            if signal["entry"] and i not in positions and i not in pending and not kill and not stale and cooldown.get(i,0)<=at+HOUR:
                source=current.get(i)
                if not source or not source["valid"]:continue
                pending[i]={"kind":"entry","active_from":at+HOUR,"signal_time":at+HOUR,
                    "limit":source["close"]*(1-strategy.get("limit_offset_pct",0)/100)}
        if kill:
            for i in positions:pending[i]={"kind":"exit","active_from":at+HOUR}
    unresolved=[]
    if strategy.get("end_policy","period_end")=="period_end":
        for i,p in list(positions.items()):
            mark=latest.get(i)
            if not mark or end-mark[1]>max_mark_age:unresolved.append(i);continue
            close(i,p,mark[0],end,"period_end")
    final=cash+sum(p["qty"]*latest[i][0] for i,p in positions.items() if i in latest)
    if equity:equity[-1]={**equity[-1],"cash":cash,"equity":final,"exposure":final-cash,"drawdown_pct":(final/peak-1)*100}
    winners=[t["net_pnl"] for t in trades if t["net_pnl"]>0];losers=[t["net_pnl"] for t in trades if t["net_pnl"]<0]
    metrics={"initial_capital":initial,"final_equity":final,"return_pct":(final/initial-1)*100,"max_drawdown_pct":min([p["drawdown_pct"] for p in equity],default=0),
        "closed_trades":len(trades),"win_rate":len(winners)/len(trades) if trades else None,"flat_trades":sum(t["net_pnl"]==0 for t in trades),
        "expectancy_usdt":statistics.mean(t["net_pnl"] for t in trades) if trades else None,"profit_factor":sum(winners)/abs(sum(losers)) if losers else None,
        "profit_factor_reason":None if losers else "no_losses","fees_usdt":sum(o.get("fee",0) for o in orders),"unresolved_positions":unresolved,
        "rounding_policy":"idealized_size_rules","execution_policy":"next_h1_open_stop_first_v1","costs":costs,"cagr":None,"sharpe":None}
    benchmark=bars.get(benchmark_id,{})
    b0=benchmark.get(start);b1=benchmark.get(end-HOUR)
    metrics["benchmark_return_pct"]=(b1["close"]/b0["open"]-1)*100 if b0 and b1 and b0["valid"] and b1["valid"] else None
    metrics["benchmark_cost_policy"]="buy-and-hold, zero benchmark transaction costs, separate from strategy"
    duration=(end-start)/DAY
    if duration>=180 and final>0 and not missing and not unresolved and all(p["quality"]=="ok" for p in equity):
        metrics["cagr"]=(final/initial)**(365.25/duration)-1
        daily=[p for p in equity if p["at"]%DAY==0]
        returns=[daily[i]["equity"]/daily[i-1]["equity"]-1 for i in range(1,len(daily)) if daily[i-1]["equity"]>0 and daily[i]["at"]-daily[i-1]["at"]==DAY]
        if len(returns)>1:
            deviation=statistics.stdev(returns)
            metrics["annualized_volatility"]=deviation*math.sqrt(365)
            metrics["sharpe"]=statistics.mean(returns)/deviation*math.sqrt(365) if deviation>0 else None
        metrics["annualization_policy"]="UTC daily equity; 365 days volatility, 365.25 days CAGR; risk-free rate zero"
    return finite_result({"status":"incomplete" if missing or unresolved else "succeeded","metrics":metrics,"trades":trades,"orders":orders,"equity":equity,
        "open_positions":positions,"missing":missing,"method_version":"lab-spot-h1-1.0"})


def compute(payload):
    data=payload["dataset"];spec=payload["spec"];strategy=payload.get("strategy")
    result=simulate(data,spec,strategy) if spec["experiment_type"]=="simulation" else descriptive(data,spec)
    result.update({"data_mode":data["data_mode"],"spec_hash":digest(spec),"dataset_hash":payload["dataset_hash"],"strategy_hash":digest(strategy) if strategy else None,
        "code_version":"research-lab-1.0","limitations":data.get("limitations",[])})
    result["result_hash"]=digest(result)
    return finite_result(result)
