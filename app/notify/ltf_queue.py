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
from ..models import now_ms
from ..models_ltf import LtfEvent
from ..services.quality import data_quality
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


class LtfDispatcher:
    """Фильтрация и доставка LtfEvent через sender.send_ltf (с кнопками)."""

    def __init__(self, db: Database, cfg: DetectorConfig, sender: Sender,
                 settings=None):
        self.db = db
        self.cfg = cfg
        self.sender = sender
        # Settings — для URL-кнопки «Открыть приложение»; None — без неё
        self.settings = settings

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
            if self._delivery_blocked(ev):
                log.info("LTF: доставка %s отложена — данные инструмента "
                         "не свежи (D02)", ev.kind)
                continue
            if self._bot_blocked(ev):
                # мьют/настройки бота (ТЗ п.9): только доставка останавливается;
                # событие помечается доставленным — после unmute не уйдёт
                self.db.mark_ltf_event_delivered(ev.id)
                continue
            ctx = self._load_context(ev)
            texts = render_ltf_messages(ev, ctx)
            if not texts:
                continue
            markup = self._keyboard(ev, ctx)
            try:
                for i, text in enumerate(texts):
                    # кнопки — на завершающей части (там футер со ссылкой)
                    await self.sender.send_ltf(
                        text,
                        reply_markup=markup if i == len(texts) - 1 else None,
                    )
            except Exception:  # noqa: BLE001 — любая ошибка транспорта = ретрай
                log.warning("LTF: доставка события %s не удалась, будет ретрай",
                            ev.id, exc_info=True)
                continue
            self.db.mark_ltf_event_delivered(ev.id)
            sent += 1
        return sent

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

    async def __call__(self, events: list[LtfEvent]) -> None:
        await self.deliver(events)
