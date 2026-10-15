"""Durable, ordered quote intake with bounded calculation and publication batches."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional
from ..adapters.base import QuoteTick
from ..models import Event, Instrument, now_ms
from .metrics import get_registry

log = logging.getLogger(__name__)
DEGRADED_PREFIX = "quote:degraded:"

@dataclass
class _Entry:
    instrument_id: int
    price: float
    ts: int
    price_kind: str
    source: str
    event_at: Optional[int]
    received_at: int
    source_seq: Optional[str]
    row_id: Optional[int] = None

    @classmethod
    def from_tick(cls, iid, tick, source):
        return cls(iid,tick.price,tick.monotonic_at,tick.price_kind,source,tick.event_at,tick.received_at,tick.source_seq)

    @classmethod
    def from_spool(cls, row):
        return cls(row['instrument_id'],row['price'],row['event_at'] if row['event_at'] is not None else row['received_at'],row['price_kind'],row['source'],row['event_at'],row['received_at'],row['source_seq'],row['id'])

class _InstrumentState:
    def __init__(self, iid):
        self.iid = iid
        self.queue = deque()
        self.seen = {}
        self.seen_order = deque()
        self.has_items = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.consumer = None
        self.ins = None
        self.spooled = 0
        self.degraded = False

class MarketQueue:
    def __init__(self, worker, *, queue_capacity=500, dedup_capacity=256, spool_batch=50):
        self.worker, self.db = worker, worker.db
        self.queue_capacity = int(queue_capacity)
        self.dedup_capacity = int(dedup_capacity)
        self.spool_batch = max(1,int(spool_batch))
        self._states = {}
        self._started = self._stopping = False
        self._metrics = get_registry()
        for name in ('market_queue_enqueued','market_queue_spooled','market_queue_drained','market_queue_duplicates','market_queue_errors'):
            self._metrics.register(name,'counter',('venue',))
        self._metrics.register('market_queue_depth','gauge',())
        self._metrics.register('market_queue_wait','histogram',('venue',))

    def _state(self, iid):
        state = self._states.get(iid)
        if state is None:
            state = _InstrumentState(iid)
            self._states[iid] = state
        return state

    @staticmethod
    def _dedup_key(source,tick):
        return (source,tick.source_seq) if tick.source_seq is not None else (source,tick.event_at if tick.event_at is not None else tick.received_at,tick.price)

    def note_ingested(self, iid,tick,source):
        st=self._state(iid)
        key=self._dedup_key(source,tick)
        if key in st.seen: return True
        st.seen[key]=None
        st.seen_order.append(key)
        while len(st.seen_order)>self.dedup_capacity:
            st.seen.pop(st.seen_order.popleft(),None)
        return False

    def enqueue(self, ins,tick,source, *, persisted_id=None):
        if self._stopping: raise RuntimeError('quote_intake_stopping')
        if persisted_id is None:
            persisted_id=self.db.spool_quote(ins.id,source=source,event_at=tick.event_at,received_at=tick.received_at,price=tick.price,price_kind=tick.price_kind,source_seq=tick.source_seq)
        st=self._state(ins.id)
        st.ins=ins
        st.idle.clear()
        st.spooled=self.db.spool_counts().get(ins.id,0)
        overflow=st.spooled>self.queue_capacity
        if overflow:
            if not st.degraded:
                # AC11: переполнение видно и после рестарта (meta для UI);
                # best-effort — при транзитном busy флаг допишет следующий тик
                try:
                    self.db.set_meta(f'{DEGRADED_PREFIX}{ins.id}',json.dumps({'since_ms':now_ms(),'reason':'overflow'}))
                except Exception:
                    log.warning('market_queue: degraded-метка overflow для %s не записана (busy)',ins.id)
            st.degraded=True
        self._metrics.counter('market_queue_spooled' if overflow else 'market_queue_enqueued',labels={'venue':ins.venue})
        self._update_depth()
        self._ensure_consumer(st)
        return 'spooled' if overflow else 'queued'

    def start(self):
        if self._started: return
        self._started=True
        ids=set(self.db.spool_counts())
        ids.update(r['instrument_id'] for r in self.db.conn.execute('SELECT DISTINCT instrument_id FROM market_publication WHERE published_at IS NULL'))
        for iid in ids:
            st=self._state(iid)
            st.ins=self.db.get_instrument(iid)
            st.spooled=self.db.spool_counts().get(iid,0)
            st.idle.clear()
            self._ensure_consumer(st)
        for key in self.db.meta_prefix(DEGRADED_PREFIX):
            if int(key.rsplit(':',1)[1]) not in ids:
                self.db.delete_meta(key)

    async def stop(self):
        self._stopping=True
        for st in self._states.values(): st.has_items.set()
        await asyncio.gather(*(st.consumer for st in self._states.values() if st.consumer),return_exceptions=True)

    async def wait_idle(self,iid,timeout):
        try:
            await asyncio.wait_for(self._state(iid).idle.wait(),timeout)
            return True
        except asyncio.TimeoutError: return False

    def checkpoint(self, iid):
        row = self.db.conn.execute(
            'SELECT MAX(id) AS id FROM quote_spool WHERE instrument_id=?', (iid,)).fetchone()
        return int(row['id'] or 0)

    async def wait_through(self, iid, checkpoint, timeout):
        """Wait for the accepted prefix, without waiting for future live ticks."""
        deadline = time.monotonic() + timeout
        while True:
            row = self.db.conn.execute(
                "SELECT 1 FROM quote_spool WHERE instrument_id=? AND id<=? "
                "AND status NOT IN ('processed','cancelled') LIMIT 1", (iid, checkpoint)).fetchone()
            if row is None:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            await asyncio.sleep(min(.05, left))

    def _ensure_consumer(self,st):
        st.has_items.set()
        if not self._stopping and (st.consumer is None or st.consumer.done()):
            st.consumer=asyncio.create_task(self._consume(st))

    async def _consume(self,st):
        try:
            while not self._stopping:
                try: await asyncio.wait_for(st.has_items.wait(),1)
                except asyncio.TimeoutError: pass
                st.has_items.clear()
                try:
                    await self._drain(st)
                except Exception:
                    self._metrics.counter('market_queue_errors',labels={'venue':self._venue(st)})
                    log.exception('quote consumer failed; durable work retained: %s',st.iid)
                    await asyncio.sleep(1)
        finally:
            st.consumer=None

    async def _publish(self,st):
        for row in self.db.pending_publications(st.iid):
            events=[self.db.get_event(i) for i in json.loads(row['event_ids'])]
            events=[e for e in events if e is not None]
            await self.worker._dispatch_events(events)
            if st.ins is not None: await self.worker._ltf_on_poll(st.ins,events)
            await asyncio.to_thread(self._ack_publication,row['id'])

    def _ack_publication(self,row_id):
        with self.db.batch_writes():
            self.db.conn.execute('UPDATE market_publication SET published_at=? WHERE id=?',(now_ms(),row_id))

    async def _drain(self,st):
        await self._publish(st)
        started=time.monotonic()
        for row in self.db.load_spool(st.iid,limit=self.spool_batch):
            if self._stopping: break
            if row['status']=='quarantine' or row['next_attempt_at']>now_ms():
                st.degraded=True
                break # strict ordering: never skip a failed market observation
            entry=_Entry.from_spool(row)
            try:
                await self._process(st,entry)
            except Exception as exc:
                st.degraded=True
                await asyncio.to_thread(self.db.fail_quote,entry.row_id,f'{type(exc).__name__}: {exc}')
                await asyncio.to_thread(self.db.set_meta,f'{DEGRADED_PREFIX}{st.iid}',json.dumps({'since_ms':now_ms(),'reason':'calculation_failed','row_id':entry.row_id}))
                break
            await self._publish(st)
            if time.monotonic()-started>=.1: break
        st.spooled=self.db.spool_counts().get(st.iid,0)
        if not st.spooled and not self.db.pending_publications(st.iid):
            st.degraded=False
            await asyncio.to_thread(self.db.delete_meta,f'{DEGRADED_PREFIX}{st.iid}')
            st.idle.set()
        else:
            first=self.db.load_spool(st.iid,limit=1)
            if first and first[0]['status'] != 'quarantine' and first[0]['next_attempt_at']<=now_ms():
                st.has_items.set()
        self._update_depth()

    async def _process(self,st,entry):
        venue=self._venue(st)
        self._metrics.histogram('market_queue_wait',max(0,(now_ms()-entry.received_at)/1000),{'venue':venue})
        def calculate_then_receipt():
            # AC02 (§8): расчёт БЕЗ охватывающей write-транзакции — пока движок
            # считает (возможно, долго), блокировка писателя SQLite не держится,
            # и durable-persist другого инструмента не ждёт этот расчёт.
            # Движок батчит свои записи сам (_transactional-методы), а крах
            # между фазами безопасен: строка spool остаётся pending, повторная
            # обработка идемпотентна (Scanner._emit дедуп по
            # (zone,cycle,kind,occurred_at); доставка дедупится в _record_pending).
            # Транзакционным остаётся только короткий receipt.
            row=self.db.conn.execute('SELECT status FROM quote_spool WHERE id=?',(entry.row_id,)).fetchone()
            if row is not None and row['status'] in {'processed','cancelled'}: return []
            instrument=self.db.get_instrument(entry.instrument_id)
            if instrument is None or not instrument.enabled:
                with self.db.batch_writes():
                    self.db.cancel_quote_work(entry.instrument_id)
                return []
            events=self.worker.scanner.on_price(entry.instrument_id,entry.price,entry.ts,True,set(self.worker.scan_tfs),entry.price_kind)
            with self.db.batch_writes():
                # Повторная проверка ПОСЛЕ расчёта: за время него строку могли
                # отменить (инструмент отключён) — публикацию не пишем; события
                # уже в журнале (идемпотентны), их подхватит штатный путь
                row=self.db.conn.execute('SELECT status FROM quote_spool WHERE id=?',(entry.row_id,)).fetchone()
                if row is not None and row['status'] in {'processed','cancelled'}: return []
                event_ids=[]
                for event in events:
                    if event.id is None:
                        stored=self.db.conn.execute("SELECT id FROM event WHERE zone_id=? AND cycle_id=? AND kind=? AND occurred_at=?",
                            (event.zone_id,event.cycle_id,event.kind.value,event.occurred_at)).fetchone()
                        if stored is None: raise RuntimeError("Market event missing from journal")
                        event_ids.append(stored["id"])
                    else:
                        event_ids.append(event.id)
                self.db.delete_spool([entry.row_id])
                if event_ids:
                    self.db.conn.execute('INSERT INTO market_publication(instrument_id,event_ids,created_at) VALUES(?,?,?)',(entry.instrument_id,json.dumps(event_ids),now_ms()))
                return events
        async with self.worker.data_sync.instrument(entry.instrument_id):
            result=await self.worker._calculate(calculate_then_receipt, calculation_instrument=entry.instrument_id)
        self._metrics.counter('market_queue_drained',labels={'venue':venue})
        return result

    def _venue(self,st): return st.ins.venue if st.ins is not None else None

    def _update_depth(self):
        self._metrics.gauge('market_queue_depth',max((s.spooled for s in self._states.values()),default=0))

    def diagnostics(self):
        return {'capacity':self.queue_capacity,'durable':True,'instruments':{str(i):{'depth':s.spooled,'spooled':s.spooled,'degraded':s.degraded,'consumer_running':s.consumer is not None and not s.consumer.done(),'idle':s.idle.is_set()} for i,s in self._states.items()}}
