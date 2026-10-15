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
    # §10.3 (R46): уведомление об исправлении исходных данных — дельта по
    # активному сигналу, не массовая рассылка истории
    "correction": "cancellation",
    # §9: смысловые переходы машины контекста (opposing/resumed/коррекция)
    "context_transition": "bos_sms",
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

    def _asset_off(self, ev: LtfEvent) -> bool:
        """Выключенный актив не уведомляем, даже если событие уже в очереди."""
        from .suppress import instrument_off

        obs = self.db.get_ltf_observation(ev.observation_id)
        if obs is None:
            return False
        return instrument_off(self.db, obs.instrument_id)

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
        """График H1 в оформлении сайта, зафиксированный на время события.

        Минимум неделя данных даёт контекст перед текущей ногой; видимое
        окно выбирает общий с сайтом H1Layers.currentFrame.
        """
        if self.settings is None or ctx.observation is None:
            return None
        from .h1_image import render_h1_event_chart
        from .chart_snapshot import (
            chart_meta_matches,
            chart_signature,
            find_saved_chart,
            write_chart_meta,
        )

        obs = ctx.observation
        from ..services.h1_setup import local_leg_at
        leg = local_leg_at(self.db, obs.instrument_id, ev.occurred_at)
        if leg is not None and leg.get("origin_at"):
            span = max(0, ev.occurred_at - int(leg["origin_at"]))
            days = max(2, min(14, int(span / 86_400_000) + 2))
        else:
            elapsed_days = (
                (ev.occurred_at - obs.activated_at) / 86_400_000
                if obs.activated_at else 0
            )
            days = 3 if elapsed_days <= 3 else (7 if elapsed_days <= 7 else 14)
        # Правая граница — время события. Повтор доставки не рисует новый файл
        # и не пересчитывает историю на текущих свечах.
        signature = chart_signature(ev)
        out_dir = Path(self.settings.db_path).parent / "charts"
        out = out_dir / f"ltf_event_{ev.id}_{signature[:12]}.png"
        if chart_meta_matches(out, signature):
            return str(out)
        reused = find_saved_chart(out_dir, signature)
        if reused:
            return reused
        path = await asyncio.to_thread(
            render_h1_event_chart, self.db, obs.id, str(out),
            period_days=days, settings=self.settings,
            event=ev,
        )
        if path:
            write_chart_meta(Path(path), signature, int(ev.id), int(ev.occurred_at))
        return path

    def _packet_events(self, members):
        return [ev for m in members if not m["reason"] and (ev := self.db.get_ltf_event(m["event_id"])) is not None]

    def _entry_block(self, ev, ctx, cache=None):
        from ..services.h1_setup import entry_block_reason
        from .ltf_projection import present_event
        reason = entry_block_reason(self.db, ev, now_ms(), cfg=self.cfg)
        if reason:
            return reason
        if ev.kind == "entries_ready" and ev.payload.get("entries"):
            shown = present_event(self.db, self.cfg, ev, ctx, cache=cache, current=True)
            if not shown.payload.get("entries"):
                return "no current movement zones"
        return None

    def _validate_packet(self, row, members):
        events = self._packet_events(members)
        active, cache = [], {}
        for ev in events:
            reason = ("asset disabled" if self._asset_off(ev) else
                      "muted, historical or stale" if ev.delayed or not self._group_enabled(ev.kind)
                      or self._bot_blocked(ev) or self._event_stale(ev) else
                      self._entry_block(ev, self._load_context(ev), cache))
            if reason:
                self.db.conn.execute(
                    "UPDATE notification_member SET reason=? WHERE packet_id=? AND event_id=?",
                    (reason, row["id"], ev.id))
            else:
                active.append(ev)
        self.db.conn.commit()
        if not active:
            return "asset disabled" if events and all(self._asset_off(e) for e in events) else "muted, historical or stale"
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
        from .telegram import tradingview_url
        events = self._packet_events(members)
        if not events:
            import json
            return Card(**json.loads(row["card"]))
        validation_cache = {}
        active = [e for e in events if not self._event_stale(e) and not self._bot_blocked(e)
                  and not self._delivery_blocked(e) and not self._asset_off(e)
                  and not self._entry_block(e, self._load_context(e), validation_cache)]
        # A same-close zone delta may have been added before/after the BOS.
        # Always name the physical break as the headline of the combined card.
        lead = next((e for e in active if e.kind in ("bos", "sms")),
                    active[0] if active else events[0])
        ctx = self._load_context(lead)
        targets, details = [], []
        all_events = [ev for m in members if (ev := self.db.get_ltf_event(m["event_id"])) is not None]
        for ev in all_events:
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
                        from urllib.parse import urlencode
                        params = urlencode({"zone": c.zone.id, "token": self.settings.auth_token})
                        target["url"] = f"{base}/?{params}"
                if target not in targets:
                    targets.append(target)
        import json
        old = json.loads(row["card"])
        path = old.get("image_path")
        needs_chart = self.settings is not None and (lead.kind in _CHART_KINDS or lead.kind == "entries_ready")
        if needs_chart and ((path is None and row["status"] != "sent")
                            or (row["status"] == "sent" and row["dirty"])):
            try:
                summary = await asyncio.to_thread(self._summary_event, active or [lead], lead)
                path = await self._render_event_chart(summary, ctx) or path
            except Exception:
                log.warning("LTF chart unavailable", exc_info=True)
        text = await asyncio.to_thread(self._packet_text, active or [lead], lead, ctx)
        from .compact import delivery_delay
        delay = delivery_delay(lead.occurred_at, now_ms())
        if delay:
            text += "\n" + delay
        if lead.kind not in ("entries_ready", "range_ready", "touch"):
            from dataclasses import replace
            if self._delivery_blocked(replace(lead, kind="entries_ready")):
                text += "\n⚠️ Данные сейчас неактуальны · показан факт на время события"
        if needs_chart and not path:
            text += "\n📊 График временно недоступен"
        if len(targets) > 1 and path:
            text += "\n📊 На графике — указанное основание HTF; выбор других — по кнопке"
        return Card(text, "\n\n".join(details), path, targets, needs_chart and not path)

    def _summary_event(self, events, lead):
        from dataclasses import replace
        from .compact import ordered_entries
        from .ltf_projection import present_event
        cache = {}
        shown = [present_event(self.db, self.cfg, event, self._load_context(event),
                               cache=cache, current=event.kind == "entries_ready") for event in events]
        entries = ordered_entries([entry for event in shown for entry in event.payload.get("entries", [])])
        projected_lead = next(e for e in shown if e.id == lead.id)
        excluded = [entry for event in shown for entry in event.payload.get("excluded_entries", [])]
        return replace(projected_lead, payload={**projected_lead.payload, "entries": entries,
                                               "excluded_entries": excluded})

    def _packet_text(self, events, lead, ctx):
        """Обычный BOS остаётся прежним текстом. Карточка перехода — когда
        один слом отменяет сценарии или подтверждает разворот."""
        from .compact import ltf_text
        shared = [e for e in events if (e.payload or {}).get("market_transition_key")]
        use_transition = (
            len(shared) > 1
            or any(e.kind == "cancellation" and (e.payload or {}).get("market_transition_key") for e in shared)
            or any((e.payload or {}).get("reversal_confirmed") for e in events)
        )
        if not use_transition:
            combined = self._summary_event(events, lead)
            return ltf_text(combined, ctx, len({e.observation_id for e in events}))
        from ..services.htf_context import reversal_projection, transition_card_text
        from ..services.h1_setup import project_setup, setup_card_lines
        view = None
        if ctx.observation is not None:
            view = reversal_projection(
                self.db, ctx.observation.instrument_id, lead.occurred_at, self.cfg,
            )
        text = transition_card_text(events, ctx, view)
        if ctx.observation is not None:
            snap = project_setup(
                self.db, self.cfg, ctx.observation.instrument_id, lead.occurred_at,
                mode="event",
            )
            extra = setup_card_lines(snap)
            if extra:
                text = text + "\n" + "\n".join(extra)
        return text

    async def deliver(self, events: list[LtfEvent]) -> int:
        pending = await asyncio.to_thread(self._enqueue, events)
        await self.outbox.flush_inline()
        return sum(bool(self.db.get_ltf_event(i).delivered) for i in pending)

    def _enqueue(self, events: list[LtfEvent]) -> list[int]:
        from .outbox import Card
        from .compact import ltf_key
        pending, cache = [], {}
        for ev in sorted(events, key=lambda e: e.kind not in ("bos", "sms")):
            stored = self.db.get_ltf_event(ev.id) if ev.id is not None else None
            if stored is None or ev.delayed:
                continue
            if (not self._group_enabled(ev.kind) or self._event_stale(ev)
                    or self._bot_blocked(ev) or self._asset_off(ev)):
                self.db.mark_ltf_event_delivered(ev.id)
                continue
            ctx = self._load_context(ev)
            if self._entry_block(ev, ctx, cache):
                self.db.mark_ltf_event_delivered(ev.id)
                continue
            key = ltf_key(ev, ctx)
            existing = self.db.conn.execute(
                "SELECT id,semantic_key FROM notification_packet WHERE channel='ltf' AND destination=? AND semantic_key=?",
                (self.outbox.destination, key)).fetchone()
            # A zone delta on the same close extends the structural card,
            # including a late arrival, rather than generating a second alert.
            if ev.kind in ("entries_ready", "bos", "sms") and existing is None:
                combined = self._same_close_packet(ev, ctx)
                if combined is not None:
                    existing, key = combined, combined["semantic_key"]
            # Журнальный delivered общий. Другой получатель всё равно ставит свой пакет.
            if stored.delivered and existing:
                continue
            historic_match = False
            if not existing and ctx.instrument:
                rows = self.db.conn.execute(
                    "SELECT e.id FROM ltf_event e JOIN ltf_observation o ON o.id=e.observation_id "
                    "WHERE o.instrument_id=? AND e.kind=? AND e.occurred_at=? AND e.delivered=1 AND e.delayed=0",
                    (ctx.instrument.id, ev.kind, ev.occurred_at)).fetchall()
                for prior in rows:
                    old = self.db.get_ltf_event(prior["id"])
                    if ltf_key(old, self._load_context(old)) != key:
                        continue
                    owned = self.db.conn.execute(
                        """SELECT p.id,p.semantic_key,p.status FROM notification_member m
                           JOIN notification_packet p ON p.id=m.packet_id
                           WHERE m.event_id=? AND p.destination=? AND p.channel='ltf'
                             AND p.status IN ('sent','suppressed')""",
                        (old.id, self.outbox.destination),
                    ).fetchone()
                    any_packet = self.db.conn.execute(
                        "SELECT 1 FROM notification_member WHERE event_id=?",
                        (old.id,),
                    ).fetchone()
                    # До outbox событие уже ушло одному владельцу: повторно не шлём.
                    # Пакет другого получателя этот адрес не закрывает.
                    if owned and owned["status"] == "sent":
                        existing, key = owned, owned["semantic_key"]
                        break
                    if owned or any_packet is None:
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
        return pending

    def _same_close_packet(self, ev, ctx):
        from .compact import ltf_key
        from .ltf_projection import direction_of
        if ctx.instrument is None:
            return None
        kinds = "('bos','sms')" if ev.kind == "entries_ready" else "('entries_ready')"
        rows = self.db.conn.execute(
            "SELECT DISTINCT p.id,p.semantic_key,e.id AS event_id FROM notification_packet p "
            "JOIN notification_member m ON m.packet_id=p.id AND m.channel='ltf' "
            "JOIN ltf_event e ON e.id=m.event_id JOIN ltf_observation o ON o.id=e.observation_id "
            "WHERE p.destination=? AND p.channel='ltf' AND p.status IN "
            "('pending','failed','sent','sending','bundled','uncertain') "
            f"AND e.kind IN {kinds} AND e.occurred_at=? AND o.instrument_id=? "
            "AND m.reason IS NULL ORDER BY p.id",
            (self.outbox.destination, ev.occurred_at, ctx.instrument.id)).fetchall()
        matches = {}
        for row in rows:
            other = self.db.get_ltf_event(row["event_id"])
            context = self._load_context(other)
            if direction_of(other, context) == direction_of(ev, ctx):
                if ev.kind in ("bos", "sms"):
                    attached = self._packet_events(self.outbox.members(row["id"]))
                    if any(e.kind in ("bos", "sms") and ltf_key(e, self._load_context(e)) != ltf_key(ev, ctx)
                           for e in attached):
                        continue
                matches.setdefault(ltf_key(other, context), row)
        # Two distinct breaks on the same bar must remain distinguishable.
        return next(iter(matches.values())) if len(matches) == 1 else None

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
        await self.outbox.flush_inline()
        return queued

    async def __call__(self, events: list[LtfEvent]) -> None:
        await self.deliver(events)
