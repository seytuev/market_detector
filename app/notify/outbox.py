"""Persistent presentation outbox. Market journals remain the source of truth.

Network timeouts are ambiguous: never automatically resend a possibly accepted
Telegram message. Expired send leases also become uncertain after a crash.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field

from ..models import now_ms

log = logging.getLogger(__name__)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass
class Card:
    text: str
    details: str = ""
    image_path: str | None = None
    # Each target is explicit; grouped cards never silently act on the first.
    targets: list[dict] = field(default_factory=list)
    chart_pending: bool = False


class Outbox:
    def __init__(self, db, sender, cfg):
        self.db, self.sender, self.cfg = db, sender, cfg
        self.validators = {}
        self.finishers = {}
        self.renderers = {}
        self.lock = asyncio.Lock()
        self.register("chart", lambda r, m: None, lambda i, s: None)
        self._media_check = 0

    @property
    def destination(self):
        return str(getattr(self.sender, "chat_id", "owner"))

    def register(self, channel, validate, finish, render=None):
        self.validators[channel] = validate
        self.finishers[channel] = finish
        if render:
            self.renderers[channel] = render

    def get(self, packet_id):
        return self.db.conn.execute("SELECT * FROM notification_packet WHERE id=?",
                                    (packet_id,)).fetchone()

    def members(self, packet_id):
        return self.db.conn.execute(
            "SELECT * FROM notification_member WHERE packet_id=? ORDER BY id",
            (packet_id,)).fetchall()

    def put(self, channel, key, card, members, *, quiet=False):
        """Idempotent enqueue; late equivalent contexts update a sent card."""
        stamp = now_ms()
        due = stamp + int(getattr(self.cfg, "notification_digest_seconds", 900)) * 1000 if quiet else stamp
        encoded = json.dumps(asdict(card), ensure_ascii=False)
        with self.db.conn._lock:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO notification_packet "
                "(destination,channel,semantic_key,card,quiet,created_at,due_at) VALUES(?,?,?,?,?,?,?)",
                (self.destination, channel, key, encoded, int(quiet), stamp, due))
            row = self.db.conn.execute(
                "SELECT * FROM notification_packet WHERE destination=? AND channel=? AND semantic_key=?",
                (self.destination, channel, key)).fetchone()
            added = False
            for event_id in members:
                cur = self.db.conn.execute(
                    "INSERT OR IGNORE INTO notification_member(packet_id,channel,event_id) VALUES(?,?,?)",
                    (row["id"], channel, event_id))
                added |= cur.rowcount > 0
            if added and row["status"] in ("sent", "sending"):
                self.db.conn.execute("UPDATE notification_packet SET dirty=1 WHERE id=?", (row["id"],))
                if row["parent_id"]:
                    self.db.conn.execute("UPDATE notification_packet SET dirty=1 WHERE id=?", (row["parent_id"],))
            self.db.conn.commit()
        return row["id"]

    def suppress(self, packet_id, reason):
        self.db.conn.execute(
            "UPDATE notification_packet SET status='suppressed',reason=? WHERE id=? AND status IN ('pending','failed')",
            (reason, packet_id))
        self.db.conn.commit()

    def _finish(self, row, status):
        if row["channel"] == "chart" and status == "sent" and row["semantic_key"].startswith("media:"):
            parent_id = int(row["semantic_key"].split(":")[1])
            for member in self.members(parent_id):
                if member["channel"] == "ltf":
                    self.db.update_ltf_event_chart(member["event_id"], "sent")
        fn = self.finishers.get(row["channel"])
        if fn:
            for member in self.members(row["id"]):
                fn(member["event_id"], "suppressed" if member["reason"] else status)

    async def _card(self, row):
        render = self.renderers.get(row["channel"])
        card = await render(row, self.members(row["id"])) if render else Card(**json.loads(row["card"]))
        self.db.conn.execute("UPDATE notification_packet SET card=? WHERE id=?",
                             (json.dumps(asdict(card), ensure_ascii=False), row["id"]))
        self.db.conn.commit()
        return card

    def _failure(self, row, exc):
        retry = getattr(exc, "retry_after", None)
        if retry is not None:
            seconds = retry.total_seconds() if hasattr(retry, "total_seconds") else float(retry)
            status, delay = "failed", max(1, seconds)
        elif isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {"TimedOut", "NetworkError"}:
            status, delay = "uncertain", 0
        else:
            status = "failed" if row["attempts"] < 5 else "exhausted"
            delay = min(3600, 30 * 2 ** row["attempts"])
        self.db.conn.execute(
            "UPDATE notification_packet SET status=?,due_at=?,reason=? WHERE id=?",
            (status, now_ms() + int(delay * 1000), f"{type(exc).__name__}: {exc}", row["id"]))
        self.db.conn.commit()
        log.warning("Notification %s: %s", row["id"], status)

    async def flush(self):
        async with self.lock:
            stamp = now_ms()
            self._recover_completed()
            self.db.conn.execute(
                "UPDATE notification_packet SET status='uncertain',reason='send lease expired' "
                "WHERE destination=? AND status='sending' AND lease_until<?", (self.destination, stamp))
            self.db.conn.commit()
            # No unbounded catch-up burst: bounded work; remaining packets stay durable.
            rows = self.db.conn.execute(
                "SELECT * FROM notification_packet WHERE destination=? AND "
                "((status IN ('pending','failed') AND due_at<=?) OR (status='sent' AND dirty=1)) "
                "ORDER BY quiet,created_at LIMIT 100", (self.destination, stamp)).fetchall()
            quiet = []
            for row in rows:
                if row["channel"] not in self.validators and row["channel"] != "digest":
                    continue  # dispatcher not registered yet
                if row["status"] == "sent":
                    try:
                        card = await self._card(row)
                        if row["message_id"]:
                            await self.sender.edit_card(row["message_id"], card, row["id"], photo=bool(row["photo"]))
                        self.db.conn.execute("UPDATE notification_packet SET dirty=0 WHERE id=?", (row["id"],))
                        self.db.conn.commit()
                        self._finish(row, "sent")
                    except Exception:
                        log.warning("Unable to update notification %s", row["id"], exc_info=True)
                    continue
                verdict = self.validators.get(row["channel"], lambda r, m: None)(row, self.members(row["id"]))
                if verdict == "wait":
                    continue
                if verdict:
                    self.suppress(row["id"], verdict)
                    self._finish(row, "suppressed")
                    continue
                if row["quiet"] and row["channel"] not in ("digest", "chart"):
                    quiet.append(row)
                    continue
                await self._send(row)
            if quiet:
                period = int(getattr(self.cfg, "notification_digest_seconds", 900)) * 1000
                last = self.db.conn.execute(
                    "SELECT MAX(created_at) FROM notification_packet WHERE destination=? AND channel='digest'",
                    (self.destination,)).fetchone()[0]
                if last is None or stamp - last >= period:
                    # Include the entire current quiet window, not one message
                    # every five seconds as individual due times expire.
                    extra = self.db.conn.execute(
                        "SELECT * FROM notification_packet WHERE destination=? AND quiet=1 "
                        "AND channel NOT IN ('digest','chart') AND status='pending' ORDER BY id",
                        (self.destination,)).fetchall()
                    selected = {r["id"]: r for r in quiet}
                    for r in extra:
                        validate = self.validators.get(r["channel"])
                        if validate and validate(r, self.members(r["id"])) is None:
                            selected[r["id"]] = r
                    await self._digest(list(selected.values()))

    def _recover_completed(self):
        # Crash after Telegram success was committed, before journal ack:
        # finish locally, never send the packet again.
        self.db.conn.execute(
            "UPDATE notification_packet SET status='sent' WHERE status='bundled' AND parent_id IN "
            "(SELECT id FROM notification_packet WHERE destination=? AND channel='digest' AND status='sent')",
            (self.destination,))
        self.db.conn.commit()
        for channel, table in (("ltf", "ltf_event"), ("alt", "alt_event")):
            if channel not in self.finishers:
                continue
            rows = self.db.conn.execute(
                f"SELECT DISTINCT p.* FROM notification_packet p JOIN notification_member m ON m.packet_id=p.id "
                f"JOIN {table} e ON e.id=m.event_id WHERE p.destination=? AND p.channel=? "
                "AND p.status IN ('sent','suppressed') AND e.delivered=0", (self.destination, channel)).fetchall()
            for row in rows:
                self._finish(row, row["status"])
        if "htf" in self.finishers:
            rows = self.db.conn.execute(
                "SELECT DISTINCT p.* FROM notification_packet p JOIN notification_member m ON m.packet_id=p.id "
                "JOIN delivery d ON EXISTS (SELECT 1 FROM json_each(d.event_ids) j WHERE j.value=m.event_id) "
                "WHERE p.destination=? AND p.channel='htf' AND p.status IN ('sent','suppressed') "
                "AND d.status IN ('pending','failed')", (self.destination,)).fetchall()
            for row in rows:
                self._finish(row, row["status"])

    async def _send(self, row):
        # A single conditional UPDATE is the cross-process claim.
        cur = self.db.conn.execute(
            "UPDATE notification_packet SET status='sending',attempts=attempts+1,lease_until=? "
            "WHERE id=? AND status IN ('pending','failed') AND due_at<=?",
            (now_ms() + 300_000, row["id"], now_ms()))
        self.db.conn.commit()
        if not cur.rowcount:
            return
        row = self.get(row["id"])
        try:
            if row["channel"] == "digest":
                verdict = self.validate_digest(row)
                if verdict:
                    self.db.conn.execute("UPDATE notification_packet SET status=?,due_at=? WHERE id=?",
                        ("failed" if verdict == "wait" else "suppressed", now_ms() + 30_000, row["id"]))
                    self.db.conn.commit()
                    return
            card = await self._card(row)
            message_id = await self.sender.send_card(card, row["id"], quiet=bool(row["quiet"]))
        except Exception as exc:
            self._failure(row, exc)
            return
        self.db.conn.execute(
            "UPDATE notification_packet SET status='sent',message_id=?,photo=?,sent_at=? WHERE id=?",
            (message_id, int(bool(card.image_path)), now_ms(), row["id"]))
        self.db.conn.commit()
        self._finish(row, "sent")
        if row["channel"] == "digest":
            children = self.db.conn.execute("SELECT * FROM notification_packet WHERE parent_id=? AND status='bundled'", (row["id"],)).fetchall()
            for child in children:
                self.db.conn.execute("UPDATE notification_packet SET status='sent',sent_at=? WHERE id=?",
                                     (now_ms(), child["id"]))
                self._finish(child, "sent")
            self.db.conn.commit()

    async def _digest(self, rows):
        # Freeze membership before sending. Retry uses this very same packet.
        key = fingerprint([r["id"] for r in rows])
        packet_id = self.put("digest", key, Card("🔕 Сводка LevelFrame"), [], quiet=False)
        with self.db.conn._lock:
            for row in rows:
                self.db.conn.execute(
                    "UPDATE notification_packet SET parent_id=?,status='bundled' WHERE id=? AND status IN ('pending','failed')",
                    (packet_id, row["id"]))
            self.db.conn.execute("UPDATE notification_packet SET quiet=1 WHERE id=?", (packet_id,))
            self.db.conn.commit()
        await self._send(self.get(packet_id))

    def validate_digest(self, row):
        children = self.db.conn.execute("SELECT * FROM notification_packet WHERE parent_id=? AND status='bundled'", (row["id"],)).fetchall()
        valid, waiting = 0, False
        for child in children:
            verdict = self.validators.get(child["channel"], lambda r, m: "wait")(child, self.members(child["id"]))
            if verdict == "wait":
                waiting = True
            elif verdict:
                self.db.conn.execute("UPDATE notification_packet SET status='suppressed',reason=? WHERE id=?",
                                     (verdict, child["id"]))
                self._finish(child, "suppressed")
            else:
                valid += 1
        self.db.conn.commit()
        return "wait" if waiting else (None if valid else "empty digest")

    async def render_digest(self, row, members):
        children = self.db.conn.execute("SELECT * FROM notification_packet WHERE parent_id=? AND status IN ('bundled','sent') ORDER BY id", (row["id"],)).fetchall()
        blocks, details, targets = [], [], []
        for child in children:
            verdict = None if child["status"] == "sent" else self.validators.get(child["channel"], lambda r, m: "wait")(child, self.members(child["id"]))
            if verdict:
                continue
            card = Card(**json.loads(child["card"])) if child["status"] == "sent" else await self._card(child)
            blocks.append(card.text)
            details.append(card.details or card.text)
            targets.extend(card.targets)
        return Card("🔕 Сводка LevelFrame\n\n" + "\n\n".join(blocks), "\n\n".join(details), targets=targets)

    async def retry_media(self):
        if now_ms() < self._media_check:
            return 0
        self._media_check = now_ms() + 60_000
        rows = self.db.conn.execute(
            "SELECT * FROM notification_packet WHERE destination=? AND status='sent' "
            "AND json_extract(card,'$.chart_pending')=1 LIMIT 20", (self.destination,)).fetchall()
        queued = 0
        for row in rows:
            renderer = self.renderers.get(row["channel"])
            if not renderer:
                continue
            previous = json.loads(row["card"])
            key = f"notification:media:{row['id']}"
            state = json.loads(self.db.get_meta(key) or "{}")
            if state.get("attempts", 0) >= 5 or state.get("due_at", 0) > now_ms():
                continue
            attempts = state.get("attempts", 0) + 1
            self.db.set_meta(key, json.dumps({"attempts": attempts, "due_at": now_ms() + 60_000 * 2 ** attempts}))
            try:
                card = await renderer({**dict(row), "status": "media_retry"}, self.members(row["id"]))
            except Exception:
                log.warning("Media retry failed for %s", row["id"], exc_info=True)
                continue
            if not card.image_path:
                continue
            media = Card("📊 График к событию\n" + previous["text"].split("\n")[0],
                         previous["details"], card.image_path, previous["targets"])
            packet_id = self.put("chart", f"media:{row['id']}", media, [], quiet=False)
            self.db.conn.execute("UPDATE notification_packet SET quiet=1 WHERE id=?", (packet_id,))
            previous["chart_pending"] = False
            self.db.conn.execute("UPDATE notification_packet SET card=? WHERE id=?",
                                 (json.dumps(previous, ensure_ascii=False), row["id"]))
            self.db.conn.commit()
            queued += 1
        return queued

    async def run(self):
        while True:
            try:
                await self.flush()
                await self.retry_media()
            except Exception:
                log.exception("Notification outbox flush failed")
            await asyncio.sleep(5)


def get_outbox(db, sender, cfg):
    boxes = getattr(db, "_notification_boxes", None)
    if boxes is None:
        boxes = db._notification_boxes = {}
    key = id(sender)
    if key not in boxes:
        box = boxes[key] = Outbox(db, sender, cfg)
        box.renderers["digest"] = box.render_digest
    return boxes[key]
