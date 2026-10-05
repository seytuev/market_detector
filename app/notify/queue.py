"""Очередь доставки уведомлений (§8, §9, §11 п.5).

EventDispatcher получает события, УЖЕ записанные движком в таблицу event,
и отвечает только за доставку: фильтрация повторов, объединение одновременных
сигналов в одно сообщение, идемпотентность и повтор неуспешных доставок.
Дубли рыночных событий dispatcher не создаёт никогда.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ..config import DetectorConfig
from ..db import Database
from ..models import (
    Delivery,
    Event,
    Instrument,
    Zone,
    ZoneStatus,
    now_ms,
)
from .suppress import (
    BOT_GRP_HTF,
    BOT_GRP_SERVICE,
    bot_delivery_blocked,
    mark_delivered,
    should_notify,
)

log = logging.getLogger(__name__)

# §11 п.7: сколько последних свечей ТФ зоны попадает на снимок для Telegram
CHART_CANDLES = 75  # ~2,5 месяца D1: ближе к текущим свечам, зона читается


@dataclass
class EventView:
    """Одно событие внутри пакета: объект и причина не скрываются (§9)."""
    event: Event
    zone: Optional[Zone]
    instrument: Optional[Instrument]


@dataclass
class MessagePayload:
    """Готовое к отправке сообщение: одно или несколько событий одного вызова."""
    events: list[Event]
    zones: list[Zone]
    views: list[EventView]
    user: str = "owner"
    image_path: Optional[str] = None  # PNG из chartimg (§11 п.7), если сгенерирован


class Sender(Protocol):
    """Транспорт доставки. Исключение из send = неуспешная доставка (ретрай)."""

    async def send(self, payload: MessagePayload) -> None: ...

    async def send_text(self, text: str) -> None:
        """Сервисное сообщение владельцу (§11): без кнопок и снимка."""
        ...

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        """LTF-сигнал с inline-кнопками навигации (ТЗ бота п.10);
        reply_markup=None — часть длинного сообщения без кнопок."""
        ...


class EventDispatcher:
    """Фильтрация, объединение и идемпотентная доставка событий."""

    def __init__(self, db: Database, cfg: DetectorConfig, sender: Sender,
                 destination: str = "telegram", user: str = "owner",
                 charts_dir: Optional[str] = None,
                 chat_id: Optional[str] = None):
        self.db = db
        self.cfg = cfg
        self.sender = sender
        self.destination = destination
        self.user = user
        # Каталог для снимков зон (§11 п.7); None — снимки не генерируются
        self.charts_dir = charts_dir
        # Владелец бота для настроек доставки (ТЗ п.9: мьют/группы/watchlist);
        # None — фильтры бота не применяются (старое поведение)
        self.chat_id = chat_id

    # ---------- внутреннее ----------

    def _load_view(self, event: Event) -> EventView:
        zone = self.db.get_zone(event.zone_id)
        instrument = (
            self.db.get_instrument(zone.instrument_id) if zone is not None else None
        )
        return EventView(event=event, zone=zone, instrument=instrument)

    def _passes_filters(self, view: EventView) -> bool:
        # §10: режим «уведомлять только о подтверждённых» — авто-кандидаты молчат
        if (
            self.cfg.notify_only_reviewed
            and view.zone is not None
            and view.zone.source == "auto"
            and view.zone.status == ZoneStatus.CANDIDATE
        ):
            return False
        return should_notify(self.db, view.event, self.cfg, self.user)

    def _bot_blocked(self, view: EventView) -> bool:
        """Настройки бота владельца (ТЗ п.9): мьют/группы/watchlist.
        Подавленное событие помечается доставленным без отправки — после
        unmute накопившееся не уходит."""
        zone = view.zone
        return bot_delivery_blocked(
            self.db, self.chat_id,
            grp=BOT_GRP_HTF, kind=view.event.kind.value,
            instrument_id=zone.instrument_id if zone is not None else None,
            zone_id=view.event.zone_id,
        )

    def _record_pending(self, event: Event) -> Optional[int]:
        """Пишет delivery pending по UNIQUE idempotency_key.

        None = доставка с таким ключом уже зафиксирована: технический ретрай
        той же обработки, НЕ новое рыночное событие (§8/§9) — пропускаем.
        """
        key = event.idempotency_key(self.user)
        # Явная проверка ключа: у sqlite3 lastrowid не сбрасывается после
        # проигнорированного INSERT OR IGNORE, поэтому полагаемся на SELECT.
        exists = self.db.conn.execute(
            "SELECT 1 FROM delivery WHERE idempotency_key=?", (key,)
        ).fetchone()
        if exists:
            return None
        return self.db.record_delivery(
            Delivery(
                id=None,
                event_ids=[event.id],
                destination=self.destination,
                status="pending",
                idempotency_key=key,
            )
        )

    async def _attach_image(self, payload: MessagePayload) -> None:
        """§11 п.7: снимок графика зоны первого события пакета.

        Рендер CPU-bound (matplotlib) — в отдельном потоке; сбой генерации
        не должен ломать доставку текста.
        """
        if self.charts_dir is None or not payload.views:
            return
        view = payload.views[0]
        zone, ins = view.zone, view.instrument
        if zone is None or zone.id is None or ins is None:
            return
        candles = self.db.get_candles(zone.instrument_id, zone.timeframe)
        if not candles:
            return
        out = (
            Path(self.charts_dir)
            / f"zone_{zone.id}_c{zone.cycle_id}_{now_ms()}.png"
        )
        source = f"{ins.venue} {ins.market_type} / {ins.symbol}"
        try:
            from .chartimg import render_zone_chart  # тяжёлый импорт — лениво

            payload.image_path = await asyncio.to_thread(
                render_zone_chart, candles[-CHART_CANDLES:], zone, out, source
            )
        except Exception:
            log.warning(
                "не удалось сгенерировать снимок зоны %s", zone.id, exc_info=True
            )

    # ---------- публичное API ----------

    async def dispatch(self, events: list[Event]) -> list[Delivery]:
        """Доставляет события одного вызова одним объединённым сообщением.

        Возвращает доставки, зафиксированные этим вызовом (по одной на каждое
        событие пакета — §9: пакет не скрывает отдельные объекты/причины).
        """
        views: list[EventView] = []
        delivery_ids: list[int] = []
        blocked: list[tuple[EventView, int]] = []
        for event in events:
            view = self._load_view(event)
            if not self._passes_filters(view):
                continue
            delivery_id = self._record_pending(event)
            if delivery_id is None:
                continue  # идемпотентность: такая доставка уже есть
            if self._bot_blocked(view):
                blocked.append((view, delivery_id))
                continue
            views.append(view)
            delivery_ids.append(delivery_id)

        delivered_at = now_ms()
        if blocked:
            # мьют/выключение останавливает ТОЛЬКО доставку: событие
            # помечается доставленным, чтобы после unmute старые события
            # не ушли (ТЗ п.9 «не рассылать накопившиеся»)
            for view, delivery_id in blocked:
                self.db.update_delivery(delivery_id, "sent", delivered_at=delivered_at)
                mark_delivered(self.db, view.event, delivered_at, self.user)

        if not views:
            return self._deliveries_by_ids(
                delivery_ids + [d for _, d in blocked]
            )

        payload = MessagePayload(
            events=[v.event for v in views],
            zones=[v.zone for v in views if v.zone is not None],
            views=views,
            user=self.user,
        )
        await self._attach_image(payload)
        try:
            await self.sender.send(payload)
        except Exception as exc:  # noqa: BLE001 — любая ошибка транспорта = ретрай
            error = f"{type(exc).__name__}: {exc}"
            for delivery_id in delivery_ids:
                self.db.update_delivery(delivery_id, "failed", error=error)
            return self._deliveries_by_ids(
                delivery_ids + [d for _, d in blocked]
            )

        for view, delivery_id in zip(views, delivery_ids):
            self.db.update_delivery(delivery_id, "sent", delivered_at=delivered_at)
            mark_delivered(self.db, view.event, delivered_at, self.user)
        return self._deliveries_by_ids(
            delivery_ids + [d for _, d in blocked]
        )

    async def notify_service(self, text: str) -> None:
        """Сервисное уведомление владельцу (§11): не рыночное событие,
        но идёт общим транспортом и пишется в журнал delivery (event_ids=[]).
        Неуспешные сервисные доставки не ретраятся — текст не хранится."""
        log.warning("SERVICE: %s", text)
        # ключ включает текст: разные сообщения в одну миллисекунду —
        # разные записи журнала (ретрая сервисных доставок нет, дедуп не нужен)
        delivery_id = self.db.record_delivery(
            Delivery(
                id=None,
                event_ids=[],
                destination=self.destination,
                status="pending",
                idempotency_key=f"service:{self.user}:{now_ms()}:{text}",
            )
        )
        # ТЗ п.9: выключенная группа «Сервис» глушит сервисные сообщения
        # (мьют /mute all на них НЕ действует — только торговые события);
        # подавленное помечается доставленным — после включения не уйдёт
        if bot_delivery_blocked(
            self.db, self.chat_id, grp=BOT_GRP_SERVICE, kind="service"
        ):
            if delivery_id is not None:
                self.db.update_delivery(delivery_id, "sent", delivered_at=now_ms())
            return
        try:
            await self.sender.send_text(text)
        except Exception as exc:  # noqa: BLE001 — журналируем сбой, не роняем воркер
            if delivery_id is not None:
                self.db.update_delivery(
                    delivery_id, "failed", error=f"{type(exc).__name__}: {exc}"
                )
            return
        if delivery_id is not None:
            self.db.update_delivery(delivery_id, "sent", delivered_at=now_ms())

    async def retry_pending(self) -> list[Delivery]:
        """Повторяет pending/failed доставки (§11 п.5).

        Берёт только недоставленные записи, поэтому sent не дублируется.
        Сами рыночные события не пересоздаются — повторяется только доставка.
        """
        pending = self.db.pending_deliveries()
        if not pending:
            return []
        events_by_id = {e.id: e for e in self.db.get_events(limit=10000)}
        done: list[Delivery] = []
        for delivery in pending:
            if not delivery.event_ids:
                # сервисные сообщения (notify_service) не ретраятся —
                # их текст не хранится в БД
                continue
            events = [events_by_id[i] for i in delivery.event_ids
                      if i in events_by_id]
            if not events:
                self.db.update_delivery(
                    delivery.id, "failed", error="события не найдены в БД"
                )
                done.append(delivery)
                continue
            views = [self._load_view(e) for e in events]
            payload = MessagePayload(
                events=events,
                zones=[v.zone for v in views if v.zone is not None],
                views=views,
                user=self.user,
            )
            await self._attach_image(payload)
            try:
                await self.sender.send(payload)
            except Exception as exc:  # noqa: BLE001
                self.db.update_delivery(
                    delivery.id, "failed", error=f"{type(exc).__name__}: {exc}"
                )
            else:
                delivered_at = now_ms()
                self.db.update_delivery(
                    delivery.id, "sent", delivered_at=delivered_at
                )
                for event in events:
                    mark_delivered(self.db, event, delivered_at, self.user)
            done.append(delivery)
        return done

    def _deliveries_by_ids(self, ids: list[int]) -> list[Delivery]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            f"SELECT * FROM delivery WHERE id IN ({marks}) ORDER BY id", ids
        ).fetchall()
        import json

        return [
            Delivery(
                id=r["id"], event_ids=json.loads(r["event_ids"]),
                destination=r["destination"], status=r["status"],
                idempotency_key=r["idempotency_key"],
                delivered_at=r["delivered_at"], error=r["error"],
            )
            for r in rows
        ]
