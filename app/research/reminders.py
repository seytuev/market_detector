"""Opt-in reminders use the existing outbox and persisted revision keys."""
from __future__ import annotations

from html import escape
from .contracts import ResearchError, choice, decode, digest, encode, text, uid
from .catalysts import Catalysts, DAY
from ..notify.outbox import Card


class Reminders:
    def __init__(self, store, settings, outbox=None):
        self.s, self.settings, self.outbox = store, settings, outbox
        if outbox:
            outbox.register("research_catalyst", self.validate, self.finish)

    def subscriptions(self):
        return [{"id":r["id"],"active":bool(r["active"]),**decode(r["payload"])}
                for r in self.s.rows("SELECT * FROM research_subscription ORDER BY created_at,id")]

    def subscribe(self, body):
        offsets = body.get("offsets_minutes",[1440,60])
        if not isinstance(offsets,list) or not offsets or any(x not in {60,1440} for x in offsets):
            raise ResearchError("invalid_offsets", "Допустимы напоминания за сутки и за час")
        assets = body.get("asset_uids",[])
        for asset in assets:
            self.s.require_asset(asset)
        value = {"asset_uids":assets,"kinds":body.get("kinds",[]),"offsets_minutes":sorted(set(offsets)),
                 "since":self.s.clock(),"include_global":bool(body.get("include_global",False))}
        identifier = uid()
        with self.s.db.batch_writes():
            self.s.execute("INSERT INTO research_subscription(id,active,created_at,payload) VALUES(?,1,?,?)",
                           (identifier,self.s.clock(),encode(value)))
            self.s.audit("subscription",identifier,{"action":"created",**value})
        return {"id":identifier,"active":True,**value}

    def disable(self, identifier):
        with self.s.db.batch_writes():
            if not self.s.one("SELECT id FROM research_subscription WHERE id=?", (identifier,)):
                raise ResearchError("subscription_not_found","Подписка не найдена",404)
            self.s.execute("UPDATE research_subscription SET active=0 WHERE id=?", (identifier,))
            self.s.execute("UPDATE research_reminder SET status='cancelled' WHERE subscription_id=? AND status='pending'",(identifier,))
        return {"id":identifier,"active":False}

    def plan(self):
        if not self.settings.research_notifications_enabled:
            return
        now = self.s.clock()
        events = Catalysts(self.s).list(from_at=now,to_at=now+2*DAY,review_status="accepted",limit=100)["items"]
        for sub in self.subscriptions():
            if not sub["active"]:
                continue
            for event in events:
                if event["stage"] in {"rumor","denied","cancelled","live"} or event["precision"] not in {"exact","day"}:
                    continue
                if sub["asset_uids"] and not set(event["asset_uids"]).intersection(sub["asset_uids"]) and not (
                    event["scope"]=="global" and sub["include_global"]):
                    continue
                if sub["kinds"] and event["kind"] not in sub["kinds"]:
                    continue
                for offset in sub["offsets_minutes"]:
                    if event["precision"]=="day" and offset!=1440:
                        continue
                    due = event["starts_at"]-offset*60_000
                    # Subscription/backfill never bursts overdue reminders.
                    if due < sub["since"]:
                        continue
                    identifier = digest([event["id"],event["revision_id"],sub["id"],offset])
                    with self.s.db.batch_writes():
                        self.s.execute("INSERT INTO research_reminder(id,event_number,catalyst_id,revision_id,subscription_id,due_at,status,payload) "
                                       "VALUES(?,?,?,?,?,?,'pending',?) ON CONFLICT(id) DO NOTHING",
                                       (identifier,int(identifier[:15],16),event["id"],event["revision_id"],sub["id"],due,encode({"offset":offset})))
        if not self.outbox:
            return
        for row in self.s.rows("SELECT * FROM research_reminder WHERE status='pending' AND due_at<=? AND due_at>=? ORDER BY due_at LIMIT 30",
                               (now,now-300_000)):
            event = Catalysts(self.s).get(row["catalyst_id"])
            if event["revision_id"] != row["revision_id"]:
                self.s.execute("UPDATE research_reminder SET status='cancelled' WHERE id=?",(row["id"],))
                continue
            base = self.settings.effective_base_url()
            link = base.rstrip("/") + "/events.html?mode=catalysts&catalyst="+event["id"] if base else "/events.html?mode=catalysts&catalyst="+event["id"]
            card = Card(text="<b>Предстоящее событие</b>\n"+escape(event["title"])+"\n"+
                        escape(event["date_label"] or "Точное время в карточке")+"\n"+escape(event["summary"][:500]),
                        targets=[{"kind":"research_catalyst","label":"Открыть катализатор","url":link}])
            self.outbox.put("research_catalyst",row["id"],card,[row["event_number"]])
            self.s.execute("UPDATE research_reminder SET status='queued' WHERE id=?",(row["id"],))
        self.s.execute("UPDATE research_reminder SET status='missed' WHERE status='pending' AND due_at<?",(now-300_000,))

    def validate(self, packet, members):
        if len(members)!=1:
            return "Некорректное напоминание"
        member = members[0]
        row = self.s.one("SELECT r.*,s.active FROM research_reminder r JOIN research_subscription s ON s.id=r.subscription_id WHERE r.id=?",
                         (packet["semantic_key"],))
        if not self.settings.research_notifications_enabled or not row or not row["active"]:
            return "Подписка выключена"
        event = Catalysts(self.s).get(row["catalyst_id"])
        if event["revision_id"]!=row["revision_id"] or event["stage"] in {"cancelled","denied","live"}:
            return "Дата или состояние события изменились"
        return None

    def finish(self, identifier, status):
        self.s.execute("UPDATE research_reminder SET status=? WHERE event_number=?", (status,identifier))
