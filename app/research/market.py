"""Read existing market stores. Research never starts a second candle collector."""
from math import isfinite
from .contracts import ResearchError,digest,decode

HOUR=3_600_000
DAY=24*HOUR


def valid_bar(row,step):
    numbers=[row[k] for k in ("open","high","low","close")]
    return (all(isinstance(x,(float,int)) and isfinite(x) and x>0 for x in numbers)
            and row["low"]<=min(row["open"],row["close"])<=max(row["open"],row["close"])<=row["high"]
            and row["open_time"]%step==0)


class Market:
    def __init__(self,store):self.s=store

    def instrument(self,identifier,spot=True):
        if isinstance(identifier,str) and identifier.startswith("alt:"):
            try:source_id=int(identifier[4:])
            except ValueError:raise ResearchError("invalid_instrument","Некорректный источник ALT")
            row=self.s.one("SELECT * FROM alt_instrument_source WHERE id=?",(source_id,))
            if not row:raise ResearchError("instrument_not_found","Источник ALT не найден",404)
            value={"id":identifier,"symbol":row.get("symbol",""),"venue":row.get("venue"),"market_type":"spot",
                   "quote_asset":row.get("quote"),"alt_source_id":source_id,"asset_id":row.get("asset_id")}
        else:
            try:identifier=int(identifier)
            except (ValueError,TypeError):raise ResearchError("invalid_instrument","Укажите инструмент")
            instrument=self.s.db.get_instrument(identifier)
            if not instrument:raise ResearchError("instrument_not_found","Инструмент не найден",404)
            value=vars(instrument)
        if spot and (value["market_type"]!="spot" or value["quote_asset"]!="USDT"):
            raise ResearchError("unsupported_market","Методика поддерживает только spot USDT",422)
        return value

    def resolve(self,asset_uid):
        links=self.s.rows("SELECT DISTINCT instrument_id FROM research_instrument_asset WHERE asset_uid=?",(asset_uid,))
        values=[]
        for row in links:
            current=self.s.instrument_link(row["instrument_id"])
            if not current or current.get("asset_uid")!=asset_uid:continue
            try:values.append(self.instrument(row["instrument_id"]))
            except ResearchError:continue
        return values[0] if len(values)==1 else None

    def benchmark(self):
        rows=[i for i in self.s.db.get_instruments() if i.asset=="BTC" and i.market_type=="spot" and i.quote_asset=="USDT"]
        rows.sort(key=lambda i:(i.venue!="binance",i.id))
        return rows[0].id if rows else None

    def bars(self,identifier,timeframe="D1",from_at=None,to_at=None,limit=1100):
        ins=self.instrument(identifier)
        step=DAY if timeframe=="D1" else HOUR
        moment=self.s.clock() if to_at is None else min(to_at,self.s.clock())
        if ins.get("alt_source_id"):
            if timeframe!="D1":raise ResearchError("missing_history","У источника ALT есть только D1",422)
            rows=self.s.rows("SELECT * FROM alt_candle WHERE source_id=? AND open_time>=? AND open_time<=? ORDER BY open_time DESC LIMIT ?",
                (ins["alt_source_id"],from_at or 0,moment-step,limit))
            rows.reverse()
            for row in rows:row.update({"close_time":row["open_time"]+step-1,"source":ins["venue"],"revision":0})
        else:
            rows=[vars(c) for c in self.s.db.get_candles(ins["id"],timeframe,from_at,moment-step,closed_only=True,limit=limit)]
        return [{**row,"close_boundary":row["open_time"]+step,"valid":valid_bar(row,step)} for row in rows]

    def price_at(self,identifier,boundary,timeframe="H1"):
        rows=self.bars(identifier,timeframe,from_at=boundary-(HOUR if timeframe=="H1" else DAY),to_at=boundary,limit=1)
        return rows[0] if rows and rows[0]["valid"] and rows[0]["close_boundary"]==boundary else None

    def revision_fingerprint(self,identifier,timeframe,rows):
        return digest({"instrument":identifier,"timeframe":timeframe,"bars":rows})
