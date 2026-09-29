"""Доставка уведомлений окна LTF (LTF-спека §11, §14).

LtfDispatcher получает события, УЖЕ записанные движком в ltf_event
(UNIQUE(dedupe_key) — дублей нет по построению, §11.5), и отвечает только
за доставку: фильтр по группам настройки ltf_notify_kinds, пропуск delayed
(восстановленных replay), отметку delivered и ретрай недоставленных.
Отказ Telegram не отменяет факт касания: рыночное состояние пишется движком,
здесь повторяется только отправка (§9).
"""
from __future__ import annotations

import logging

from ..config import DetectorConfig
from ..db import Database
from ..models_ltf import LtfEvent
from .ltf_templates import LtfContext, render_ltf_messages
from .queue import Sender

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


class LtfDispatcher:
    """Фильтрация и доставка LtfEvent через sender.send_text."""

    def __init__(self, db: Database, cfg: DetectorConfig, sender: Sender):
        self.db = db
        self.cfg = cfg
        self.sender = sender

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

    async def deliver(self, events: list[LtfEvent]) -> int:
        """Отправляет события списка; возвращает число доставленных.

        Идемпотентность: delivered=True пропускаются; дубли событий не
        возникают благодаря UNIQUE(dedupe_key) на вставке (§11.5).
        """
        sent = 0
        for ev in events:
            if ev.delayed:
                continue
            # свежее состояние из БД: повторный вызов с тем же объектом
            # после mark_ltf_event_delivered не должен отправить дважды
            stored = self.db.get_ltf_event(ev.id) if ev.id is not None else None
            if stored is None or stored.delivered:
                continue
            if not self._group_enabled(ev.kind):
                continue
            ctx = self._load_context(ev)
            texts = render_ltf_messages(ev, ctx)
            if not texts:
                continue
            try:
                for text in texts:
                    await self.sender.send_text(text)
            except Exception:  # noqa: BLE001 — любая ошибка транспорта = ретрай
                log.warning("LTF: доставка события %s не удалась, будет ретрай",
                            ev.id, exc_info=True)
                continue
            self.db.mark_ltf_event_delivered(ev.id)
            sent += 1
        return sent

    async def retry_pending(self) -> int:
        """Ретрай недоставленных текущих событий (delivered=False, не delayed)."""
        return await self.deliver(self.db.pending_ltf_events())

    async def __call__(self, events: list[LtfEvent]) -> None:
        await self.deliver(events)
