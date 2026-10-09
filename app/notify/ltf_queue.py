"""Доставка уведомлений окна LTF (LTF-спека §11, §14).

LtfDispatcher получает события, УЖЕ записанные движком в ltf_event
(UNIQUE(dedupe_key) — дублей нет по построению, §11.5), и отвечает только
за доставку: фильтр по группам настройки ltf_notify_kinds, пропуск delayed
(восстановленных replay), отметку delivered и ретрай недоставленных.
Отказ Telegram не отменяет факт касания: рыночное состояние пишется движком,
здесь повторяется только отправка (§9).

ТЗ 07.10.2026 §7: события BOS/SMS, касание Entry Zone и отмена сценария
идут с собственным графиком H1, связанным с событием одним снимком
(правая граница — время события). Состояние графика (chart_state) ведётся
отдельно от состояния события: текст без изображения не считается успешной
доставкой графика; повторная генерация досылает картинку без повторного
рыночного уведомления.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ..config import DetectorConfig
from ..db import Database
from ..models import now_ms
from ..models_ltf import LtfEvent
from ..services.quality import data_quality
from .formatting import fmt_time_msk
from .ltf_templates import LtfContext, render_ltf_messages
from .queue import Sender
from .suppress import bot_delivery_blocked, bot_group_for_ltf_kind

log = logging.getLogger(__name__)

# §14: группа настройки доставки по виду события
_KIND_GROUP = {
    "bos": "bos_sms",
    "sms": "bos_sms",
    "entries_ready": "entries_ready",
    "range_ready": "entries_ready",
    "touch": "touch",
    "sweep_confirmed": "sweep_outcome",
    "sweep_failed": "sweep_outcome",
    "cancellation": "cancellation",
}

# §7 ТЗ 07.10.2026: виды, к которым обязателен собственный график H1 события
_CHART_KINDS = {
    "bos", "sms", "touch", "sweep_confirmed", "sweep_failed", "cancellation",
}

# Подпись фото Telegram — до 1024 символов
_CAPTION_LIMIT = 1000


class LtfDispatcher:
    """Фильтрация и доставка LtfEvent через sender.send_ltf (с кнопками)."""

    def __init__(self, db: Database, cfg: DetectorConfig, sender: Sender,
                 settings=None):
        self.db = db
        self.cfg = cfg
        self.sender = sender
        # Settings — для URL-кнопки «Открыть приложение»; None — без неё
        self.settings = settings
        from .outbox import get_outbox
        self.outbox = get_outbox(db, sender, cfg)
        self.outbox.register("ltf", self._validate_packet, self._finish_packet, self._render_packet)

    def _group_enabled(self, kind: str) -> bool:
        """§14: выключение доставки группы не меняет рыночный анализ —
        события продолжают писаться в журнал, просто не отправляются."""
        group = _KIND_GROUP.get(kind)
        if group is None:
            return False
        enabled = {s.strip() for s in self.cfg.ltf_notify_kinds.split(",")}
        return group in enabled

    def _load_context(self, ev: LtfEvent) -> LtfContext:
        """Обогащение из БД: инструмент, родительская HTF-зона, сценарий."""
        obs = self.db.get_ltf_observation(ev.observation_id)
        sc = (
            self.db.get_ltf_scenario(ev.scenario_id)
            if ev.scenario_id is not None else None
        )
        zone = self.db.get_zone(obs.zone_id) if obs is not None else None
        ins = self.db.get_instrument(obs.instrument_id) if obs is not None else None
        return LtfContext(instrument=ins, zone=zone, observation=obs, scenario=sc)

    def _delivery_blocked(self, ev: LtfEvent) -> bool:
        """D02/F03: при gap/replaying/stale новые уведомления о пригодности
        не отправляются до проверки нужных данных. Событие остаётся
        недоставленным (delivered=False) и уйдёт ретраем после
        восстановления свежести; рыночное состояние это не меняет.

        Решение — единая quality.data_quality: блокируем всё, кроме «ok»
        (включая отставание расчёта processing_lag и разрыв истории).
        Без Settings (конструктор без settings) — прежняя проверка только
        по флагам воркера replaying:/stale:."""
        if ev.kind not in ("entries_ready", "range_ready", "touch"):
            return False
        obs = self.db.get_ltf_observation(ev.observation_id)
        if obs is None:
            return False
        iid = obs.instrument_id
        if self.settings is None:
            return (
                self.db.get_meta(f"replaying:{iid}") == "1"
                or self.db.get_meta(f"stale:{iid}:H1") == "1"
            )
        return (
            data_quality(self.db, self.settings, iid, now_ms())["state"]
            != "ok"
        )

    def _event_stale(self, ev: LtfEvent) -> bool:
        """ТЗ 07.10.2026 §13.6: не отправлять просроченный текущий вход.

        Входовые события (entries_ready/range_ready/touch) проверяются на
        актуальность к моменту отправки: сценарий отменён/закрыт или зона
        входа недопустима (снятый/пройденный уровень — §3: снятая ликвидность
        не становится зоной входа). События-факты (bos/sms/sweep/cancellation)
        доставляются всегда — это история, а не предложение входа.
        """
        if ev.kind not in ("entries_ready", "range_ready", "touch"):
            return False
        sc = (
            self.db.get_ltf_scenario(ev.scenario_id)
            if ev.scenario_id is not None else None
        )
        if sc is None or sc.state in ("cancelled", "closed"):
            return True
        if ev.kind == "entries_ready" and ev.payload.get("entries"):
            entries = ev.payload["entries"]
            known = [self.db.get_ltf_entry_zone(e.get("entry_zone_id")) for e in entries]
            if all(z is not None and z.validity == "invalid" for z in known):
                return True
        if ev.kind == "touch":
            zone_id = ev.payload.get("entry_zone_id")
            zone = (
                self.db.get_ltf_entry_zone(zone_id)
                if zone_id is not None else None
            )
            if zone is not None:
                if zone.validity == "invalid":
                    return True
                # уровень уже снят/пройден — шаблон касания для него
                # запрещён (§5.5 ТЗ 07.10.2026)
                tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
                if any(
                    t.entry_zone_id == zone.id
                    and t.state in ("confirmed", "failed")
                    for t in tests
                ):
                    return True
        return False

    async def _render_event_chart(self, ev: LtfEvent,
                                  ctx: LtfContext) -> Optional[str]:
        """График события (§7/§11.1): H1, правая граница — время события,
        период 3 дня с авторасширением 7/14 от HTF-касания (§8)."""
        if self.settings is None or ctx.observation is None:
            return None
        from ..bot.charts import DEFAULT_MASK, layers_from_mask, render_ltf_chart

        obs = ctx.observation
        elapsed_days = (
            (ev.occurred_at - obs.activated_at) / 86_400_000
            if obs.activated_at else 0
        )
        days = 3 if elapsed_days <= 3 else (7 if elapsed_days <= 7 else 14)
        end = ev.occurred_at  # no candle closed after the event may leak into its snapshot
        out = (
            Path(self.settings.db_path).parent / "charts"
            / f"ltf_event_{ev.id}_{now_ms()}.png"
        )
        return await asyncio.to_thread(
            render_ltf_chart, self.db, obs.id, str(out),
            tf="H1", period_days=days,
            layers=layers_from_mask(DEFAULT_MASK),
            settings=self.settings, now=ev.occurred_at, end_ms=end,
        )

    def _packet_events(self, members):
        return [ev for m in members if not m["reason"] and (ev := self.db.get_ltf_event(m["event_id"])) is not None]

    def _validate_packet(self, row, members):
        events = self._packet_events(members)
        active = [e for e in events if not e.delayed and self._group_enabled(e.kind)
                  and not self._bot_blocked(e) and not self._event_stale(e)]
        if not active:
            return "muted, historical or stale"
        if all(self._delivery_blocked(e) for e in active):
            return "wait"
        return None

    def _finish_packet(self, event_id, status):
        stored = self.db.get_ltf_event(event_id)
        if stored is None or stored.delivered:
            return
        self.db.mark_ltf_event_delivered(event_id)
        if status == "sent":
            import json
            row = self.db.conn.execute(
                "SELECT p.card FROM notification_packet p JOIN notification_member m ON m.packet_id=p.id "
                "WHERE m.channel='ltf' AND m.event_id=? AND p.status='sent' ORDER BY p.id DESC LIMIT 1",
                (event_id,)).fetchone()
            if row:
                card = json.loads(row["card"])
                if card.get("image_path") or card.get("chart_pending"):
                    self.db.update_ltf_event_chart(event_id, "sent" if card.get("image_path") else "failed")

    async def _render_packet(self, row, members):
        from .outbox import Card
        from .compact import ltf_text
        from .telegram import tradingview_url
        events = self._packet_events(members)
        active = [e for e in events if not self._event_stale(e) and not self._bot_blocked(e)
                  and not self._delivery_blocked(e)]
        lead = active[0] if active else events[0]
        ctx = self._load_context(lead)
        targets, details = [], []
        for ev in events:
            c = self._load_context(ev)
            if c.instrument:
                details.append(f"Контекст #{ev.observation_id} · сценарий #{ev.scenario_id}\n"
                               f"Источник: {c.instrument.venue} {c.instrument.market_type} / {c.instrument.symbol}")
            details.extend(render_ltf_messages(ev, c))
            if c.instrument and c.zone and c.observation:
                from .formatting import fmt_price_ru
                target = dict(label=f"{c.zone.type.value} {c.zone.timeframe} · {fmt_price_ru(c.zone.lower)}–{fmt_price_ru(c.zone.upper)}",
                              instrument_id=c.instrument.id, zone_id=c.zone.id,
                              cycle_id=c.observation.cycle_id, kind="touch",
                              chart=f"nav:charto:{c.instrument.id}:{c.observation.id}", tv=tradingview_url(c.instrument))
                if self.settings:
                    from urllib.parse import urlparse
                    base = self.settings.effective_base_url()
                    if urlparse(base).hostname not in {None, "localhost", "127.0.0.1", "0.0.0.0", "::1"}:
                        target["url"] = f"{base}/?zone={c.zone.id}"
                if target not in targets:
                    targets.append(target)
        import json
        old = json.loads(row["card"])
        path = old.get("image_path")
        needs_chart = self.settings is not None and (lead.kind in _CHART_KINDS or lead.kind == "entries_ready")
        if needs_chart and path is None and row["status"] != "sent":
            try:
                path = await self._render_event_chart(lead, ctx)
            except Exception:
                log.warning("LTF chart unavailable", exc_info=True)
        text = ltf_text(lead, ctx, len({e.observation_id for e in events}))
        if lead.kind not in ("entries_ready", "range_ready", "touch"):
            from dataclasses import replace
            if self._delivery_blocked(replace(lead, kind="entries_ready")):
                text += "\n⚠️ Данные сейчас неактуальны · показан факт на время события"
        if needs_chart and not path:
            text += "\n📊 График временно недоступен"
        if len(targets) > 1 and path:
            label = f"{ctx.zone.type.value} {ctx.zone.timeframe}" if ctx.zone else "H1"
            text += f"\n📊 Контекст графика: {label}; остальные — по кнопке"
        return Card(text, "\n\n".join(details), path, targets, needs_chart and not path)

    async def deliver(self, events: list[LtfEvent]) -> int:
        from .outbox import Card
        from .compact import ltf_key
        pending = []
        for ev in events:
            stored = self.db.get_ltf_event(ev.id) if ev.id is not None else None
            if stored is None or stored.delivered or ev.delayed:
                continue
            if not self._group_enabled(ev.kind) or self._event_stale(ev) or self._bot_blocked(ev):
                self.db.mark_ltf_event_delivered(ev.id)
                continue
            ctx = self._load_context(ev)
            key = ltf_key(ev, ctx)
            existing = self.db.conn.execute(
                "SELECT id FROM notification_packet WHERE channel='ltf' AND destination=? AND semantic_key=?",
                (self.outbox.destination, key)).fetchone()
            historic_match = False
            if not existing and ctx.instrument:
                rows = self.db.conn.execute(
                    "SELECT e.id FROM ltf_event e JOIN ltf_observation o ON o.id=e.observation_id "
                    "WHERE o.instrument_id=? AND e.kind=? AND e.occurred_at=? AND e.delivered=1 AND e.delayed=0",
                    (ctx.instrument.id, ev.kind, ev.occurred_at)).fetchall()
                for prior in rows:
                    old = self.db.get_ltf_event(prior["id"])
                    if ltf_key(old, self._load_context(old)) == key:
                        historic_match = True
                        break
            packet_id = self.outbox.put("ltf", key, Card(""), [ev.id], quiet=ev.kind == "range_ready")
            if historic_match:
                self.outbox.suppress(packet_id, "equivalent delivered before outbox migration")
                self.db.mark_ltf_event_delivered(ev.id)
                continue
            if ev.kind != "range_ready" and ev.scenario_id:
                self.db.conn.execute(
                    "UPDATE notification_member SET reason='superseded' WHERE channel='ltf' AND event_id IN "
                    "(SELECT id FROM ltf_event WHERE kind='range_ready' AND scenario_id=? AND occurred_at<=?) "
                    "AND packet_id IN (SELECT id FROM notification_packet WHERE status IN ('pending','failed','bundled'))",
                    (ev.scenario_id, ev.occurred_at))
                self.db.conn.commit()
            pending.append(ev.id)
        await self.outbox.flush()
        return sum(bool(self.db.get_ltf_event(i).delivered) for i in pending)

    def _keyboard(self, ev: LtfEvent, ctx: LtfContext):
        """Единый набор кнопок под сигналом (ТЗ бота п.10)."""
        if ctx.instrument is None or ctx.instrument.id is None:
            return None
        from ..bot.keyboards import ltf_signal_inline  # лениво: telegram lib

        return ltf_signal_inline(
            ev.kind,
            ctx.instrument.id,
            zone_id=ctx.zone.id if ctx.zone is not None else None,
            settings=self.settings,
            observation_id=(
                ctx.observation.id if ctx.observation is not None else None
            ),
            instrument=ctx.instrument,
        )

    def _bot_blocked(self, ev: LtfEvent) -> bool:
        """Настройки бота владельца (ТЗ п.9): мьют/группы/watchlist.
        chat_id берётся из settings (модель «один владелец»); без settings —
        старое поведение (ничего не блокируется)."""
        chat_id = (
            self.settings.telegram_chat_id if self.settings is not None else None
        )
        if not chat_id:
            return False
        obs = self.db.get_ltf_observation(ev.observation_id)
        return bot_delivery_blocked(
            self.db, chat_id,
            grp=bot_group_for_ltf_kind(ev.kind), kind=ev.kind,
            instrument_id=obs.instrument_id if obs is not None else None,
            zone_id=obs.zone_id if obs is not None else None,
        )

    async def retry_pending(self) -> int:
        """Ретрай недоставленных текущих событий (delivered=False, не delayed)."""
        return await self.deliver(self.db.pending_ltf_events())

    async def retry_charts(self) -> int:
        queued = await self.outbox.retry_media()
        await self.outbox.flush()
        return queued

    async def __call__(self, events: list[LtfEvent]) -> None:
        await self.deliver(events)
