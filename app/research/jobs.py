"""Isolated durable research jobs: no work is added to the technical calc queue."""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import httpx

from .contracts import ResearchError, decode, encode, uid
from .integration import Integration
from .catalysts import Catalysts
from .sources import collect_deribit
from .reminders import Reminders
from .store import Store

log = logging.getLogger(__name__)


class ResearchRunner:
    def __init__(self, db, settings):
        self.s, self.settings = Store(db), settings
        self.integration = Integration(self.s,settings)
        self.owner = uid()
        self.stop_event = None
        self.active_job = None
        self.reminders = Reminders(self.s,settings)
        self.last_schedule = self.last_calendar = self.last_workbench = 0

    def bind_outbox(self, outbox):
        self.reminders = Reminders(self.s,self.settings,outbox)

    def enqueue(self, kind, payload=None, key="default"):
        if not self.settings.research_enabled:
            raise ResearchError("disabled","Исследовательский модуль выключен",503)
        from .workbench import KINDS
        if kind not in {"sync","collect","quarantine","channel"}|KINDS:
            raise ResearchError("unknown_job","Неизвестная задача")
        stamp = self.s.clock()
        with self.s.db.batch_writes():
            self.s.execute("INSERT INTO research_job(id,kind,dedup_key,status,next_attempt_at,created_at,updated_at,payload) "
                           "VALUES(?,?,?,'pending',?,?,?,?) ON CONFLICT DO NOTHING",
                           (uid(),kind,key,stamp,stamp,stamp,encode(payload or {})))
            return self.s.one("SELECT * FROM research_job WHERE kind=? AND dedup_key=? AND status IN ('pending','running')",(kind,key))

    def job(self, identifier):
        row = self.s.one("SELECT id,kind,status,attempts,progress,error_code,created_at,updated_at,payload FROM research_job WHERE id=?",(identifier,))
        if not row:
            raise ResearchError("job_not_found","Задание не найдено",404)
        payload=decode(row.pop("payload"));row["result"]=payload.get("result")
        return row

    def cancel(self, identifier):
        current=self.s.one("SELECT kind,payload FROM research_job WHERE id=?",(identifier,))
        if current and current["kind"]=="lab_run":
            from .lab import Lab
            Lab(self.s).cancel(decode(current["payload"])["id"])
        with self.s.db.batch_writes():
            self.s.execute("UPDATE research_job SET status='cancelled',lease_until=0,updated_at=? "
                           "WHERE id=? AND status IN ('pending','running')",(self.s.clock(),identifier))
        return self.job(identifier)

    def should_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():return True
        if self.active_job:
            row=self.s.one("SELECT status,lease_owner FROM research_job WHERE id=?",(self.active_job,))
            return not row or row["status"]!="running" or row["lease_owner"]!=self.owner
        return False

    def heartbeat(self,identifier,finished):
        while not finished.wait(20):
            try:
                self.s.execute("UPDATE research_job SET lease_until=?,updated_at=? WHERE id=? AND status='running' AND lease_owner=?",
                    (self.s.clock()+120000,self.s.clock(),identifier,self.owner))
            except Exception:
                log.exception("Research lease renewal failed")

    def step(self):
        if not self.settings.research_enabled:
            return
        now = self.s.clock()
        if now-self.last_schedule >= max(30,self.settings.research_sync_interval_seconds)*1000:
            for channel in self.s.rows("SELECT handle FROM research_channel WHERE enabled=1"):
                self.enqueue("channel",{"handle":channel["handle"]},key=channel["handle"])
            if self.settings.research_terminal_url and self.settings.research_integration_token:
                state = self.s.state("terminal")
                if now >= state.get("retry_after",0):
                    self.enqueue("sync")
            if self.settings.research_deribit_enabled:
                states = [self.s.state("deribit:"+currency) for currency in ("BTC","ETH")]
                if any(now-st.get("last_attempt_at",st.get("last_success_at",0)) > 6*3_600_000 for st in states):
                    self.enqueue("collect")
            self.last_schedule = now
        if now-self.last_calendar >= 60_000:
            with self.s.db.batch_writes():
                Catalysts(self.s).expire_dates()
            self.reminders.plan()
            self.last_calendar = now
        if now-self.last_workbench>=60_000:
            from .workbench import maintenance
            maintenance(self)
            self.last_workbench=now
        with self.s.db.batch_writes():
            if self.s.db.is_postgres:
                self.s.execute("SELECT pg_advisory_xact_lock(741852968)")
            row = self.s.one("SELECT * FROM research_job WHERE (status='pending' AND next_attempt_at<=?) "
                             "OR (status='running' AND lease_until<?) ORDER BY CASE WHEN kind IN ('channel','sync','model_probe','collect') THEN 0 "
                             "WHEN kind='playbook_backfill' AND json_extract(payload,'$.mode')='live' THEN 1 ELSE 2 END,updated_at,id LIMIT 1",(now,now))
            if not row:
                return
            changed = self.s.execute("UPDATE research_job SET status='running',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? "
                                     "WHERE id=? AND (status='pending' OR lease_until<?)",
                                     (self.owner,now+120000,now,row["id"],now)).rowcount
            if not changed:
                return
        finished=threading.Event();lease=threading.Thread(target=self.heartbeat,args=(row["id"],finished),daemon=True)
        self.active_job=row["id"];lease.start()
        try:
            payload = decode(row["payload"])
            outcome=None
            from .workbench import KINDS,dispatch
            if row["kind"] in KINDS:
                outcome=dispatch(self,row["kind"],payload)
                if outcome and outcome.get("more_work"):
                    self.s.execute("UPDATE research_job SET status='pending',attempts=0,progress=?,lease_until=0,next_attempt_at=?,payload=? WHERE id=? AND lease_owner=?",
                        (outcome.get("progress",0),self.s.clock()+1000,encode({**payload,"result":outcome}),row["id"],self.owner))
                    return self.job(row["id"])
            elif row["kind"]=="sync":
                outcome=self.integration.sync(row["id"],should_stop=lambda:self.stop_event is not None and self.stop_event.is_set())
                if outcome.get("interrupted"):
                    self.s.execute("UPDATE research_job SET status='pending',lease_until=0 WHERE id=? AND lease_owner=?",(row["id"],self.owner))
                    return self.job(row["id"])
            elif row["kind"]=="collect":
                collect_deribit(self.s,self.settings)
            elif row["kind"]=="channel":
                from .channels import Channels
                Channels(self.s).collect(payload["handle"])
            else:
                self.integration.retry_quarantine(payload["id"])
            with self.s.db.batch_writes():
                if self.stop_event is not None and self.stop_event.is_set():
                    self.s.execute("UPDATE research_job SET status='pending',lease_until=0,next_attempt_at=? WHERE id=? AND status='running' AND lease_owner=?",
                        (self.s.clock(),row["id"],self.owner))
                    return self.job(row["id"])
                if outcome is not None:
                    self.s.execute("UPDATE research_job SET payload=? WHERE id=? AND lease_owner=?",(encode({**payload,"result":outcome}),row["id"],self.owner))
                self.s.execute("UPDATE research_job SET status='succeeded',lease_until=0,updated_at=?,error_code=NULL "
                               "WHERE id=? AND status='running' AND lease_owner=?",(self.s.clock(),row["id"],self.owner))
        except (ResearchError,httpx.HTTPError,ValueError,KeyError) as error:
            code = getattr(error,"code","source_unavailable")
            delay = getattr(error,"retry_after",min(1800,30*2**min(row["attempts"],5))+random.randint(0,15))
            terminal = code=="auth_error" or row["attempts"]>=4
            with self.s.db.batch_writes():
                if row["kind"]=="sync":
                    state = self.s.state("terminal")
                    if code in {"cursor_expired","dataset_changed"}:
                        for key in ("snapshot_id","snapshot_cursor","change_cursor"):
                            state.pop(key,None)
                    self.s.set_state("terminal",{**state,"error_code":code,"retry_after":self.s.clock()+delay*1000})
                self.s.execute("UPDATE research_job SET status=?,error_code=?,next_attempt_at=?,lease_until=0,updated_at=? "
                               "WHERE id=? AND status='running' AND lease_owner=?",
                               ("failed" if terminal else "pending",code,self.s.clock()+delay*1000,self.s.clock(),row["id"],self.owner))
        finally:
            finished.set();lease.join(timeout=1);self.active_job=None
        return self.job(row["id"])

    async def run(self, stop):
        self.stop_event=stop
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.step)
            except Exception:
                # Never propagate a research failure into the market worker.
                log.exception("Research worker failed")
            try:
                await asyncio.wait_for(stop.wait(),timeout=2)
            except asyncio.TimeoutError:
                pass
