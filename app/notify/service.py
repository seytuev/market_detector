"""Structured source incidents; a recovery covers every affected stream."""
import json
import logging

from ..models import now_ms
from .outbox import Card, fingerprint
from .suppress import BOT_GRP_SERVICE, bot_delivery_blocked

log = logging.getLogger(__name__)


class ServiceNotifications:
    def __init__(self, outbox, chat_id):
        self.outbox, self.db, self.chat_id = outbox, outbox.db, chat_id
        outbox.register("service", self.validate, lambda event_id, status: None)
        outbox.register("incident", self.validate, lambda event_id, status: None, self.render)

    def validate(self, row, members):
        if bot_delivery_blocked(self.db, self.chat_id, grp=BOT_GRP_SERVICE, kind="service"):
            return "service disabled"

    def note(self, text, key=None):
        log.warning("SERVICE: %s", text)
        # One occurrence per digest interval, not per worker callback.
        period = max(1, getattr(self.outbox.cfg, "notification_digest_seconds", 900)) * 1000
        key = key or fingerprint([text, now_ms() // period])
        packet = self.outbox.put("service", key, Card("ℹ️ " + text, text), [], quiet=True)
        if self.validate(None, None):
            self.outbox.suppress(packet, "service disabled")
        return packet

    def failed(self, ins, timeframes, reason="network"):
        stamp = now_ms()
        with self.db.conn._lock:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO notification_incident(venue,reason,started_at) VALUES(?,?,?)",
                (ins.venue, reason, stamp))
            incident = self.db.conn.execute(
                "SELECT * FROM notification_incident WHERE venue=? AND reason=? AND recovered_at IS NULL",
                (ins.venue, reason)).fetchone()
            for tf in timeframes:
                self.db.conn.execute(
                    "INSERT INTO notification_incident_stream(incident_id,instrument_id,timeframe,recovered) "
                    "VALUES(?,?,?,0) ON CONFLICT(incident_id,instrument_id,timeframe) DO UPDATE SET recovered=0",
                    (incident["id"], ins.id, tf))
            self.db.conn.commit()
        packet = self.outbox.put("incident", f"down:{incident['id']}", Card(""), [incident["id"]], quiet=True)
        if self.validate(None, None):
            self.outbox.suppress(packet, "service disabled")
        self.db.conn.execute("UPDATE notification_incident SET packet_id=? WHERE id=?", (packet, incident["id"]))
        self.db.conn.execute("UPDATE notification_packet SET dirty=1 WHERE id=? AND status='sent'", (packet,))
        self.db.conn.commit()

    def recovered(self, ins, tf):
        incidents = self.db.conn.execute(
            "SELECT DISTINCT i.* FROM notification_incident i JOIN notification_incident_stream s ON s.incident_id=i.id "
            "WHERE i.recovered_at IS NULL AND s.instrument_id=? AND s.timeframe=?", (ins.id, tf)).fetchall()
        for incident in incidents:
            self.db.conn.execute(
                "UPDATE notification_incident_stream SET recovered=1 WHERE incident_id=? AND instrument_id=? AND timeframe=?",
                (incident["id"], ins.id, tf))
            remaining = self.db.conn.execute(
                "SELECT 1 FROM notification_incident_stream WHERE incident_id=? AND recovered=0", (incident["id"],)).fetchone()
            if remaining is None:
                self.db.conn.execute("UPDATE notification_incident SET recovered_at=? WHERE id=?", (now_ms(), incident["id"]))
                # Short outage: one combined incident instead of down/up pair.
                packet = self.outbox.get(incident["packet_id"]) if incident["packet_id"] else None
                if packet and packet["status"] in ("sent", "uncertain"):
                    self.outbox.put("incident", f"up:{incident['id']}", Card(""), [incident["id"]], quiet=True)
            self.db.conn.commit()

    async def render(self, row, members):
        incident = self.db.conn.execute("SELECT * FROM notification_incident WHERE id=?", (members[0]["event_id"],)).fetchone()
        streams = self.db.conn.execute(
            "SELECT s.*,i.symbol FROM notification_incident_stream s JOIN instrument i ON i.id=s.instrument_id "
            "WHERE incident_id=? ORDER BY i.symbol,s.timeframe", (incident["id"],)).fetchall()
        assets = {}
        for stream in streams:
            assets.setdefault(stream["symbol"], []).append(stream["timeframe"])
        names = "; ".join(f"{symbol} ({', '.join(tfs)})" for symbol, tfs in assets.items())
        recovered = incident["recovered_at"] is not None
        head = ("✅ Данные восстановлены" if recovered else "⚠️ Данные временно недоступны") + f" · {incident['venue']}"
        detail = "Все затронутые потоки восстановлены." if recovered else "Сигналы могут запаздывать. Проверяем восстановление."
        text = f"{head}\n{names}\n{detail}"
        return Card(text, text)
