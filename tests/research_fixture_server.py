"""Isolated browser fixture: never writes test catalysts into the user's database."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
from app.db import Database
from app.config import Settings
from app.models import Instrument,Candle,now_ms
from app.research.store import Store
from app.research.contracts import encode
from app.research.catalysts import Catalysts
from app.web.api import create_app

db=Database()
s=Store(db)
asset=s.create_asset({"display_symbol":"BTC","name":"Bitcoin","external_ids":[{"namespace":"coingecko","external_id":"bitcoin"}]})
iid=db.upsert_instrument(Instrument(None,"BTC","binance","spot","BTCUSDT","USDT"))
s.link(iid,{"asset_uid":asset["asset_uid"],"reason":"Изолированный стенд проверки интерфейса"})
s.execute("INSERT INTO research_identity_candidate VALUES(?,?,'pending',?,?)",("fixture-identity","v1",now_ms(),
    encode({"instrument_id":iid,"candidate_assets":[asset["asset_uid"]],"reason":"Проверка решения по идентичности"})))
c=Catalysts(s)
today=datetime.now(timezone.utc).date()
date=(today+timedelta(days=3)).isoformat()
e={"title":"Проверка обновления сети","summary":"Проверяемая запись изолированного стенда",
   "kind":"upgrade","stage":"announced","scope":"asset","asset_uids":[asset["asset_uid"]],
   "precision":"day","date_label":date,"timezone":"UTC"}
c.save({**e,"sources":[{"namespace":"test","url":"https://example.com/one","text":"Исходный анонс"}]})
c.save({**e,"title":"Подтверждение обновления сети","sources":[{"namespace":"test","url":"https://example.com/two","text":"Второй источник"}]})
c.save({**e,"title":"Квартальное исследование","precision":"quarter","date_label":str(today.year)+"-Q"+str((today.month-1)//3+1)})
c.candidate("fixture-candidate","fixture-revision",{**e,"title":"Кандидат на проверку"})
from app.research.strength import Strength
from app.research.ideas import Ideas
from app.research.playbook import Playbook
from app.research.lab import Lab
from app.research.jobs import ResearchRunner
from app.research.market import DAY,HOUR
boundary=now_ms()//DAY*DAY
db.insert_candles([Candle(iid,"D1",boundary-(100-k)*DAY,boundary-(99-k)*DAY-1,100+k,102+k,99+k,101+k,True,"fixture") for k in range(100)])
hour=now_ms()//HOUR*HOUR
db.insert_candles([Candle(iid,"H1",hour-(240-k)*HOUR,hour-(239-k)*HOUR-1,100+k,102+k,99+k,101+k,True,"fixture") for k in range(240)])
Strength(s).calculate([iid])
s.execute("INSERT INTO research_asset_external_id VALUES('fixture','btc',?,?,?)",(asset["asset_uid"],now_ms(),"{}"))
payload={"title":"Оригинальный анонс публичного запуска","original_text":"Команда подтвердила публичный запуск продукта и проверяемый рост использования сети.","published_at":now_ms()-60000,"source_kind":"telegram","assets":[{"entity_id":"btc","role":"subject"}]}
s.ingest("fixture",{"entity_kind":"materials","entity_id":"post","revision_id":"v1","operation":"upsert","change_seq":1,"payload":payload})
cfg=Settings(auth_token="research-test-token",research_enabled=True,research_deribit_enabled=False,research_ideas_enabled=True,
    research_strength_enabled=True,research_baskets_enabled=True,research_playbook_enabled=True,research_lab_enabled=True)
app=create_app(db,cfg)
@asynccontextmanager
async def lifespan(app):
    stop=asyncio.Event();task=asyncio.create_task(app.state.research.run(stop))
    try:yield
    finally:
        stop.set();await task;db.close()
app.router.lifespan_context=lifespan
