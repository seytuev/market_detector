"""Isolated Telegram UI fixture. Every model call uses a deterministic HTTP stub."""
import asyncio
import json
from contextlib import asynccontextmanager
import httpx
from app.config import Settings
from app.db import Database
from app.research.store import Store
from app.research.jobs import ResearchRunner
from app.research.model import Model
from app.research.telegram import TelegramFeed
import app.research.telegram as telegram_module
from app.web.api import create_app
from tests.test_telegram_feed import analysis,post

store=Store(Database())
settings=Settings(auth_token='telegram-fixture',research_enabled=True,research_deribit_enabled=False)
def response(request):
    body=json.loads(request.content)
    if body['response_format']['json_schema']['name']=='telegram_digest':
        source=json.loads(body['messages'][1]['content'])['stories'][0]
        output={'headline_ru':'Bitcoin объявил запуск тестовой сети','bullets':[{'text_ru':'Два канала сообщили о запуске. Проверьте первоисточник.','story_id':source['story_id'],'tickers':['BTC']}],'funds_ru':[],'watch_ru':['Проверить сроки и документацию']}
    else:output=analysis()
    return httpx.Response(200,json={'id':'fixture','choices':[{'message':{'content':json.dumps(output)}}],'usage':{'prompt_tokens':30,'completion_tokens':60}})
client=httpx.Client(transport=httpx.MockTransport(response))
model=Model(store,{'provider':'xai','key':'fixture-only','cheap':'fixture-model','strong':'fixture-model','daily_usd':'5','reserve_usd':'0.1'},client)
telegram_module.Model=lambda s:Model(s,model.config,client)
feed=TelegramFeed(store,settings,model)
for channel in ['alpha','bravo']:
    mid,rev=post(store,channel);feed.analyze(mid,rev)
feed.digest()
post(store,'pending',text='A new publication awaits analysis')
app=create_app(store.db,settings)
@asynccontextmanager
async def lifespan(app):
    stop=asyncio.Event();task=asyncio.create_task(app.state.research.run(stop))
    try:yield
    finally:
        stop.set();await task;client.close();store.db.close()
app.router.lifespan_context=lifespan
