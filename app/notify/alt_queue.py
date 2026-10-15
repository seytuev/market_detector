"""Доставка уведомлений модуля «Altcoins D1 accumulation» (ТЗ 07.10.2026 §18).

AltDispatcher получает события, УЖЕ записанные движком в alt_event
(UNIQUE(setup_id, event_type, source_event_id) — дублей нет по построению,
§18 outbox key), и отвечает только за доставку:

- события одного сетапа одного run объединяются в одно сообщение
  (терминальный приоритет — в alt_templates);
- фильтры бота — общий bot_delivery_blocked с группой «alt» (ТЗ п.9):
  подавленное событие помечается доставленным, после unmute накопившееся
  не уходит;
- отказ Telegram не отменяет рыночный факт: событие остаётся pending
  (delivered=0) и уходит ретраем; отметка delivered — только после
  успешной отправки;
- первичная загрузка: runner помечает исторические события delivered без
  отправки, notify-слой шлёт одну сводку render_backfill_summary —
  один раз за всё время (meta-флаг alt:backfill_summary_sent).
"""
from __future__ import annotations

import asyncio
import json

import logging
from typing import Any, Optional

from ..db import Database
from ..models import now_ms
from ..models_alt import AltEvent
from .alt_templates import AltContext, render_alt_message, render_backfill_summary
from .queue import Sender
from .suppress import BOT_GRP_ALT, bot_delivery_blocked

log = logging.getLogger(__name__)

# Meta-ключ разовой сводки первичной загрузки (§18: исторический replay
# не рассылает месяцы старых событий — только одна сводка после запуска)
BACKFILL_SUMMARY_META = "alt:backfill_summary_sent"


class AltDispatcher:
    """Фильтрация и доставка AltEvent через sender.send_alt."""

    def __init__(self, db: Database, settings, sender: Sender):
        self.db = db
        # Settings — для chat_id владельца (фильтры бота) и публичного URL
        # ссылки на график; None — без фильтров и без ссылки (dev/тесты)
        self.settings = settings
        from ..config import DetectorConfig
        from .outbox import get_outbox
        cfg = settings.detector if settings is not None else DetectorConfig()
        self.outbox = get_outbox(db, sender, cfg)
        self.outbox.register("alt", self._validate_packet, self._finish_packet, self._render_packet)
        self.outbox.register("alt_v3", self._validate_v3, self._finish_v3, self._render_v3)
        from .service import ServiceNotifications
        self.services = ServiceNotifications(self.outbox, self._chat_id(None))
        self.sender = sender

    # ---------- контекст ----------

    def _load_context(self, ev: AltEvent) -> AltContext:
        setup = self.db.get_alt_setup(ev.setup_id)
        asset = self.db.get_alt_asset(setup.asset_id) if setup else None
        source = (
            self.db.get_alt_instrument_source(setup.asset_id) if setup else None
        )
        frozen = self.db.get_alt_frozen_range(setup.range_id) if setup else None
        run = self.db.get_alt_run(ev.run_id) if ev.run_id else None
        base_url = (
            self.settings.effective_base_url()
            if self.settings is not None else None
        )
        return AltContext(
            asset=asset, source=source, setup=setup, frozen=frozen,
            run=run, base_url=base_url,
        )

    def _chat_id(self, chat_id: Optional[str]) -> Optional[str]:
        if chat_id:
            return chat_id
        if self.settings is not None:
            return self.settings.telegram_chat_id or None
        return None

    # ---------- доставка ----------

    def _bot_blocked(self, ev: AltEvent, chat_id: Optional[str]) -> bool:
        """Настройки бота владельца (ТЗ п.9): мьют all / группа «alt» /
        отдельный вид события. У альткоинов нет instrument/zone скоупов."""
        if not chat_id:
            return False
        return bot_delivery_blocked(
            self.db, chat_id, grp=BOT_GRP_ALT, kind=ev.event_type
        )

    def _events(self, members):
        return [e for m in members if not m["reason"] and (e := self.db.get_alt_event(m["event_id"])) is not None]

    def _validate_packet(self, row, members):
        if self.settings is not None and self.settings.alt_config.engine_version == "v3":
            return "superseded by W1 engine"
        events = self._events(members)
        if not events or all(self._bot_blocked(e, self._chat_id(None)) for e in events):
            return "muted or missing"
        for event in events:
            if self._bot_blocked(event, self._chat_id(None)):
                self.db.conn.execute("UPDATE notification_member SET reason='muted' WHERE packet_id=? AND event_id=?",
                                     (row["id"], event.id))
        self.db.conn.commit()
        ctx = self._load_context(events[0])
        if ctx.asset is not None and not ctx.asset.enabled:
            return "asset disabled"
        if ctx.setup is None or ctx.asset is None:
            return "missing setup"
        from .alt_templates import _ENTRY_TYPES, _TERMINAL_TYPES
        if ctx.setup.state in {"cancelled", "expired_no_retest", "targets_completed"}:
            if all(e.event_type in _ENTRY_TYPES for e in events):
                return "stale entry"
            for event in events:
                if event.event_type in _ENTRY_TYPES:
                    self.db.conn.execute(
                        "UPDATE notification_member SET reason='stale entry' WHERE packet_id=? AND event_id=?",
                        (row["id"], event.id))
            self.db.conn.commit()
        return None

    def _v3_rows(self, members):
        return [r for m in members if not m["reason"] and (r := self.db.conn.execute(
            "SELECT * FROM alt_v3_event WHERE id=?", (m["event_id"],)).fetchone()) is not None]

    def _validate_v3(self, row, members):
        from ..alt.engine_v3 import RULES_VERSION
        if self.settings is not None and self.settings.alt_config.engine_version != "v3":
            return "inactive W1 version"
        rows = self._v3_rows(members)
        if not rows:
            return "missing W1 event"
        chat = self._chat_id(None)
        for e in rows:
            structure = self.db.conn.execute(
                "SELECT id FROM alt_structure WHERE source_id=? AND structure_key=? AND rules_version=?",
                (e["source_id"], e["structure_key"], RULES_VERSION)).fetchone()
            asset = self.db.get_alt_asset(e["asset_id"])
            source = self.db.get_alt_instrument_source(e["asset_id"])
            blocked = not structure or (e["delivered"] and row["status"] != "sent") or not asset or not asset.enabled or not source or source.id != e["source_id"] or (chat and bot_delivery_blocked(self.db, chat, grp=BOT_GRP_ALT, kind=e["event_type"]))
            if blocked:
                self.db.conn.execute("UPDATE notification_member SET reason='muted or disabled' WHERE packet_id=? AND event_id=?",
                                     (row["id"], e["id"]))
        self.db.conn.commit()
        return None if self._v3_rows(self.outbox.members(row["id"])) else "muted or disabled"

    def _finish_v3(self, event_id, status):
        self.db.conn.execute("UPDATE alt_v3_event SET delivered=1 WHERE id=?", (event_id,))
        self.db.conn.commit()

    async def _render_v3(self, row, members):
        from .outbox import Card
        from pathlib import Path
        from . import alt_w1
        rows = self._v3_rows(members)
        if not rows:
            return Card("")
        ctx = await asyncio.to_thread(alt_w1.load_context, self.db, rows)
        text, details = alt_w1.message(ctx, delivered_at=now_ms())
        old = json.loads(row["card"])
        image = None
        if self.settings and ctx.frame:
            from .outbox import fingerprint
            # A packet can gain another simultaneous zone. Its image must
            # then contain the same contexts as its new caption and details.
            key = fingerprint([e["id"] for e in ctx.events])[:16]
            output = Path(self.settings.db_path).parent / "charts" / f"alt_w1_{row['id']}_{key}.png"
            if output.is_file():
                image = str(output)
            else:
                try:
                    image = await asyncio.to_thread(alt_w1.render_chart, self.db, ctx, output)
                except Exception:
                    log.warning("ALT W1 chart unavailable for packet %s", row["id"], exc_info=True)
        target = {"kind": "alt_w1", "label": (ctx.asset.symbol if ctx.asset else "ALTs") + " · база W1"}
        base_url = ((self.settings.effective_base_url() if self.settings else "") or "").rstrip("/")
        if base_url and ctx.structure_id:
            target["url"] = f"{base_url}/alt.html?base={ctx.structure_id}&tf=W1"
        card = Card(text, details, image, [target], bool(self.settings and not image))
        card.replace_image = bool(image and row["status"] == "sent" and image != old.get("image_path"))
        return card

    async def dispatch_v3(self):
        from .outbox import Card
        rows = self.db.conn.execute("SELECT * FROM alt_v3_event WHERE delivered=0 ORDER BY event_time_ms,id LIMIT 200").fetchall()
        groups = {}
        for e in rows:
            key = f"{e['source_id']}:{e['structure_key']}:{e['event_time_ms']//60000}"
            groups.setdefault(key, []).append(e["id"])
        for key, ids in groups.items():
            await asyncio.to_thread(self.outbox.put, "alt_v3", key, Card(""), ids)
        await self.outbox.flush_inline()
        return len(rows)

    def _finish_packet(self, event_id, status):
        self.db.mark_alt_event_delivered(event_id)

    async def _render_packet(self, row, members):
        import asyncio
        import json
        from pathlib import Path
        from .outbox import Card
        from .alt_templates import _render_block, _TERMINAL_TYPES, _chart_url, _footer
        from .formatting import fmt_time_msk, fmt_price_ru
        events = self._events(members)
        if not events:
            return Card(**json.loads(row["card"]))
        ctx = self._load_context(events[0])
        ranks = {"target_hit": 60, "entry_a": 50, "entry_b": 50, "bos_confirmed": 40,
                 "sms_confirmed": 40, "retest": 30, "mature_frozen": 20, "forming_started": 0}
        lead = max(events, key=lambda e: (100 if e.event_type in _TERMINAL_TYPES else ranks.get(e.event_type, 10), e.event_time_ms))
        lines = [f"{'⚪' if lead.event_type in _TERMINAL_TYPES else '🟢'} {ctx.asset.symbol} · Накопление D1"]
        block = _render_block(lead, ctx)
        # Entry/target prices must survive the compact card. Slicing at two
        # lines previously left a bare «Цели:» with no actual target levels.
        lines.extend(block if lead.event_type in {"entry_a", "entry_b", "target_hit", "breakout", "retest"} else block[:2])
        if len(events) > 1:
            lines.append(f"Ещё событий: {len(events) - 1} · в подробностях")
        if ctx.frozen and ctx.frozen.mature_at_ms <= lead.event_time_ms:
            lines.append(f"Диапазон: {fmt_price_ru(ctx.frozen.lower)}–{fmt_price_ru(ctx.frozen.upper)}")
        if any(e.event_type in {"entry_a", "entry_b", "retest"} for e in events) and lead.event_type not in _TERMINAL_TYPES:
            lines.append("Далее: проверьте условия сетапа · событие не подтверждает исполнение ордера")
        # Keep warnings about uncertain candle ordering and stale data visible.
        lines.extend(line for line in _footer(events, ctx) if line.startswith("⚠"))
        if ctx.setup.state in {"cancelled", "expired_no_retest", "targets_completed"} and lead.event_type not in _TERMINAL_TYPES:
            lines.append("Сетап уже завершён · показан факт на время события")
        lines.append(f"🕒 Событие: {fmt_time_msk(lead.event_time_ms)}")
        lines.append("STV Market Researcher")
        from .compact import delivery_delay
        delay = delivery_delay(lead.event_time_ms, now_ms())
        if delay:
            lines.append(delay)
        url = _chart_url(ctx)
        targets = [dict(label=f"{ctx.asset.symbol} · сетап #{ctx.setup.id}", url=url)] if url else []
        old = json.loads(row["card"])
        path = old.get("image_path")
        if not path and self.settings and row["status"] != "sent":
            try:
                from .alt_chart import render_alt_chart
                path = await asyncio.to_thread(render_alt_chart, self.db, ctx, lead,
                    Path(self.settings.db_path).parent / "charts" / f"alt_notify_{row['id']}.png")
            except Exception:
                log.warning("ALT chart unavailable", exc_info=True)
        original = [e for m in members if (e := self.db.get_alt_event(m["event_id"])) is not None]
        return Card("\n".join(lines), render_alt_message(original, ctx), path, targets, bool(self.settings and not path))

    async def _deliver_group(self, events: list[AltEvent], ctx: AltContext,
                             chat_id: Optional[str]) -> int:
        from .outbox import Card
        if ctx.asset is not None and not ctx.asset.enabled:
            for ev in events:
                await asyncio.to_thread(self.db.mark_alt_event_delivered, ev.id)
            await self.outbox.flush_inline()
            return 0
        allowed = []
        for ev in events:
            if self._bot_blocked(ev, chat_id):
                await asyncio.to_thread(self.db.mark_alt_event_delivered, ev.id)
            else:
                allowed.append(ev)
        if not allowed:
            return 0
        quiet = all(e.event_type in {"forming_started", "data_stale"} for e in allowed)
        await asyncio.to_thread(self.outbox.put, "alt", f"{allowed[0].setup_id}:{allowed[0].run_id}", Card(""),
                        [e.id for e in allowed], quiet=quiet)
        await self.outbox.flush_inline()
        return sum(bool(self.db.get_alt_event(e.id).delivered) for e in allowed)

    async def dispatch_pending(self, chat_id: Optional[str] = None) -> int:
        """Отправляет недоставленные события; возвращает число доставленных.

        Группировка — (setup_id, run_id): §18 «объединять события одного
        сетапа за run»; ретрай недоставленной пачки сохраняет её состав."""
        pending = self.db.pending_alt_events()
        if not pending:
            return 0
        chat = self._chat_id(chat_id)
        groups: dict[tuple[int, Optional[int]], list[AltEvent]] = {}
        for ev in pending:
            groups.setdefault((ev.setup_id, ev.run_id), []).append(ev)
        sent = 0
        for evs in groups.values():
            ctx = self._load_context(evs[0])
            if ctx.setup is None or ctx.asset is None:
                # строка без сетапа/актива — не копим бесконечные ретраи
                log.warning(
                    "ALT: события %s без сетапа/актива — закрыты без отправки",
                    [e.id for e in evs],
                )
                for ev in evs:
                    await asyncio.to_thread(self.db.mark_alt_event_delivered, ev.id)
                continue
            sent += await self._deliver_group(evs, ctx, chat)
        return sent

    async def retry_pending(self, chat_id: Optional[str] = None) -> int:
        """Ретрай недоставленных (delivered=0) — то же чтение pending,
        повторяется только доставка, рыночные события не пересоздаются."""
        return await self.dispatch_pending(chat_id)

    # ---------- сводка первичной загрузки (§18) ----------

    def _setups_by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for asset in self.db.list_alt_assets(enabled_only=True):
            for s in self.db.list_alt_setups(asset.id):
                counts[s.state] = counts.get(s.state, 0) + 1
        return counts

    async def notify_backfill_summary(self, summary: dict[str, Any]) -> bool:
        """Разовая сводка после первичной загрузки (§18).

        True — сводка отправлена. Один раз за всё время работы модуля:
        meta-флаг ставится только после успешной отправки; сбой транспорта
        позволяет повторить при следующем прогоне."""
        if self.db.get_meta(BACKFILL_SUMMARY_META):
            return False
        text = render_backfill_summary(summary, self._setups_by_state())
        self.services.note(text, key="alt:backfill-summary")
        # Enqueue is durable; no historical event replay on the next run.
        self.db.set_meta(BACKFILL_SUMMARY_META, str(now_ms()))
        await self.outbox.flush_inline()
        return True
