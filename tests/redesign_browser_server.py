"""Redesign browser fixture. In-memory DB and fake providers only."""
from tests.data_sync_browser_server import app, db, settings
from tests.test_alt_api import _asset, _source, _candles, _setup
from tests.test_events_api_v2 import make_fetcher, _liq_rows
from app.events.runner import run_cycle
from app.models import Zone, ZoneType, ZoneStatus, Direction, Event, EventKind, now_ms
from app.models_alt import AltState, AltEvent, AltRun

settings.auth_token = 'dev-token'

now = now_ms()
zone = db.insert_zone(Zone(None,1,ZoneType.FVG,Direction.BULL,'D1',98,102,
                          now-100000,now-90000,status=ZoneStatus.ACTIVE))
for i in range(8):
    db.insert_event(Event(None,zone,1,EventKind.TOUCH,now-60000+i*1000,now,100))
for cmc, symbol in [(5001,'AAA'),(5002,'BBB')]:
    asset = _asset(db,cmc,symbol,cmc-4990)
    source = _source(db,asset.id,'binance')
    db.conn.execute('UPDATE alt_instrument_source SET symbol=? WHERE id=?',(symbol+'USDT',source.id))
    _candles(db,source.id)
    setup = _setup(db,asset,source,AltState.ACTIVE_CONFIRMED.value,
                   flags={'structure_event':True})
    db.insert_alt_event(AltEvent(None,setup.id,'bos_confirmed',f'fixture:{cmc}',
                                event_time_ms=now-10000,detected_at_ms=now))
db.insert_alt_run(AltRun(None,now,now,status='ok',finished_ms=now,processed=2))
settings.events_basket_static = 'BTC,ETH,SOL'
settings.events_notify_enabled = False
run_cycle(db,settings,now_ms=now,fetcher=make_fetcher(now,drift=-0.0001,
    liq_rows=_liq_rows(now,hot_windows=2,hot_long=6000)))
