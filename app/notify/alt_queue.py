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

    async def _deliver_group(self, events: list[AltEvent], ctx: AltContext,
                             chat_id: Optional[str]) -> int:
        """Одна пачка (setup_id, run_id) → одно сообщение.

        Возвращает число доставленных событий. Подавленные настройками бота
        помечаются доставленными без отправки (накопившееся после unmute
        не уходит); неуспешная отправка оставляет события pending — ретрай."""
        allowed: list[AltEvent] = []
        for ev in events:
            if self._bot_blocked(ev, chat_id):
                self.db.mark_alt_event_delivered(ev.id)
            else:
                allowed.append(ev)
        if not allowed:
            return 0
        text = render_alt_message(allowed, ctx)
        try:
            await self.sender.send_alt(text)
        except Exception:  # noqa: BLE001 — любая ошибка транспорта = ретрай
            log.warning(
                "ALT: доставка событий %s не удалась, будет ретрай",
                [e.id for e in allowed], exc_info=True,
            )
            return 0
        for ev in allowed:
            self.db.mark_alt_event_delivered(ev.id)
        return len(allowed)

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
                    self.db.mark_alt_event_delivered(ev.id)
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
        try:
            await self.sender.send_text(text)
        except Exception:  # noqa: BLE001 — сводка не рыночное событие
            log.warning("ALT: сводка первичной загрузки не отправлена",
                        exc_info=True)
            return False
        self.db.set_meta(BACKFILL_SUMMARY_META, str(now_ms()))
        return True
