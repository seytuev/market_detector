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
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    now_ms,
)
from ..services.zone_groups import group_members_by_zone
from .chart_series import screenshot_candles
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

# ТЗ 07.10.2026 §13.6: «входовые» виды событий — для них доставка проверяет
# актуальность зоны на момент отправки. События-факты об инвалидации
# (LEVEL_TAKEN, BREAKER_ARCHIVED, FVG_FILLED и сервисные) не входят сюда и
# доставляются всегда.
_ENTRY_KINDS = {
    EventKind.APPROACH,
    EventKind.TOUCH,
    EventKind.DEPTH_50,
    EventKind.DEPTH_90,
    EventKind.FVG_WEAKENED,
}

# Статусы, при которых зона не может быть текущей возможностью входа.
_STALE_STATUSES = {
    ZoneStatus.TAKEN,
    ZoneStatus.ARCHIVED,
    ZoneStatus.REJECTED,
    ZoneStatus.CONVERTED,
    ZoneStatus.WORKED,
}


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
    # ТЗ 07.10.2026 §4.1: порог приближения — настройка, показываем её в тексте
    approach_pct: float = 0.02
    # §10: zone_id -> прочие зоны той же визуальной группы (для пометки
    # в тексте, что зона визуально объединена с соседними)
    group_members: dict[int, list[Zone]] = field(default_factory=dict)


class Sender(Protocol):
    """Транспорт доставки. Исключение из send = неуспешная доставка (ретрай)."""

    async def send_card(self, card, packet_id: int, *, quiet: bool = False) -> int: ...

    async def edit_card(self, message_id: int, card, packet_id: int, *, photo: bool = False) -> None: ...

    async def send(self, payload: MessagePayload) -> None: ...

    async def send_text(self, text: str) -> None:
        """Сервисное сообщение владельцу (§11): без кнопок и снимка."""
        ...

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        """LTF-сигнал с inline-кнопками навигации (ТЗ бота п.10);
        reply_markup=None — часть длинного сообщения без кнопок."""
        ...

    async def send_ltf_photo(self, image_path: str, caption: str,
                             reply_markup=None) -> None:
        """LTF-событие с собственным графиком H1 (ТЗ 07.10.2026 §7):
        фото с подписью; reply_markup=None — сопроводительное фото без
        кнопок (кнопки уезжают на текстовой части)."""
        ...

    async def send_alt(self, text: str) -> None:
        """Событие модуля «Altcoins D1 accumulation» (ТЗ 07.10.2026 §18):
        текст со ссылкой на график сетапа, без кнопок."""
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
        from .outbox import get_outbox
        self.outbox = get_outbox(db, sender, cfg)
        self.outbox.register("htf", self._validate_packet, self._finish_packet, self._render_packet)
        from .service import ServiceNotifications
        self.services = ServiceNotifications(self.outbox, chat_id)

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

    def _entry_stale(self, view: EventView) -> bool:
        """ТЗ 07.10.2026 §13.6: зона могла стать невалидной между постановкой
        уведомления и отправкой — просроченный текущий вход не отправляем.
        Просроченным считается входовое событие по зоне, которая к моменту
        отправки снята/архивна/невалидна или потеряла допуск к входу."""
        if view.event.kind not in _ENTRY_KINDS or view.zone is None:
            return False
        zone = view.zone
        if zone.status in _STALE_STATUSES or zone.market_validity != "active":
            return True
        return not zone.entry_eligible

    def _group_members(self, views: list[EventView]) -> dict[int, list[Zone]]:
        """§10: состав визуальных групп для зон пакета (только пометка в
        тексте — сами зоны и правила уведомлений не меняются).

        Группировка та же, что на графике (/api/zones/grouped): пересекающиеся
        актуальные ACTIVE-зоны инструмента."""
        members: dict[int, list[Zone]] = {}
        instrument_ids = {v.zone.instrument_id for v in views if v.zone is not None}
        for iid in instrument_ids:
            zones = [z for z in self.db.get_zones(
                instrument_id=iid, statuses=[ZoneStatus.ACTIVE])
                if z.is_currently_relevant()]
            members.update(group_members_by_zone(zones))
        return members

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
        live = 0 <= now_ms() - view.event.occurred_at <= self.cfg.delivery_target_seconds * 1000
        candles = screenshot_candles(
            self.db, zone.instrument_id, zone.timeframe, limit=CHART_CANDLES,
            end_ms=None if live else view.event.occurred_at,
        )
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
                render_zone_chart, candles, zone, out, source,
                event_at=view.event.occurred_at, event_price=view.event.price,
            )
        except Exception:
            log.warning(
                "не удалось сгенерировать снимок зоны %s", zone.id, exc_info=True
            )

    # ---------- публичное API ----------

    _QUIET = {EventKind.APPROACH, EventKind.DEPTH_50, EventKind.DEPTH_90,
              EventKind.FVG_WEAKENED, EventKind.ALREADY_IN_ZONE}

    def _packet_views(self, members):
        return [self._load_view(e) for m in members
                if (e := self.db.get_event(m["event_id"])) is not None]

    def _active_views(self, members):
        return self._packet_views([m for m in members if not m["reason"]])

    def _validate_packet(self, row, members):
        views = self._active_views(members)
        if not views:
            return "missing events"
        allowed = [v for v in views if not self._bot_blocked(v) and not self._entry_stale(v)
                   and not v.event.delayed]
        if not allowed:
            return "muted, historical or stale"
        for view in views:
            if view not in allowed:
                self.db.conn.execute("UPDATE notification_member SET reason='muted or stale' WHERE packet_id=? AND event_id=?",
                                     (row["id"], view.event.id))
        self.db.conn.commit()
        return None

    def _finish_packet(self, event_id, status):
        ev = self.db.get_event(event_id)
        if ev is None:
            return
        existing = self.db.conn.execute("SELECT status FROM delivery WHERE idempotency_key=?", (ev.idempotency_key(self.user),)).fetchone()
        if existing and existing["status"] in ("sent", "stale"):
            return
        self.db.conn.execute(
            "UPDATE delivery SET status=?,delivered_at=? WHERE idempotency_key=?",
            ("sent" if status == "sent" else "stale", now_ms(), ev.idempotency_key(self.user)))
        self.db.conn.commit()
        if status == "sent":
            mark_delivered(self.db, ev, now_ms(), self.user)

    async def _render_packet(self, row, members):
        from .outbox import Card
        from .telegram import render_text, _render_event_details, tradingview_url
        from urllib.parse import urlparse
        views = self._packet_views(members)
        allowed = [v for v in self._active_views(members) if not self._bot_blocked(v) and not self._entry_stale(v)]
        # Keep every fact in Details; show only the furthest state of a zone.
        ranks = {"approach": 0, "already_in_zone": 1, "touch": 2,
                 "depth_50": 3, "fvg_weakened": 3, "depth_90": 4,
                 "fvg_filled": 100, "ob_invalidated": 100, "breaker_archived": 100,
                 "prb_archived": 100, "level_taken": 100}
        lead = {}
        for view in allowed:
            key = view.event.zone_id
            prev = lead.get(key)
            if prev is None or ranks.get(view.event.kind.value, 5) >= ranks.get(prev.event.kind.value, 5):
                lead[key] = view
        visible = sorted(lead.values(), key=lambda v: -ranks.get(v.event.kind.value, 5))
        payload = MessagePayload(events=[v.event for v in visible], zones=[v.zone for v in visible if v.zone],
                                 views=visible, approach_pct=self.cfg.approach_pct)
        # Multiple objects: graph explicitly depicts the first, named on image;
        # every other object is selectable in the packet's graph menu.
        import json
        payload.image_path = json.loads(row["card"]).get("image_path")
        if not payload.image_path:
            await self._attach_image(payload)
        text = render_text(payload)
        if self.charts_dir and not payload.image_path:
            text += "\n📊 График временно недоступен"
        if len(visible) > 1 and payload.image_path:
            first = visible[0]
            from .formatting import fmt_price_ru
            text += f"\n📊 На графике: {first.zone.type.value} {first.zone.timeframe} · {fmt_price_ru(first.zone.lower)}–{fmt_price_ru(first.zone.upper)}"
        targets = []
        base = getattr(self.sender, "site_base_url", "")
        for v in visible:
            if not v.zone or not v.instrument:
                continue
            z, ins = v.zone, v.instrument
            from .formatting import fmt_price_ru
            prices = fmt_price_ru(z.lower) if z.is_level else f"{fmt_price_ru(z.lower)}–{fmt_price_ru(z.upper)}"
            target = dict(label=f"{ins.symbol} · {z.type.value} {z.timeframe} · {prices}",
                          zone_id=z.id, instrument_id=ins.id, cycle_id=v.event.cycle_id,
                          kind=v.event.kind.value, chart=f"nav:chartz:{z.id}", tv=tradingview_url(ins))
            if urlparse(base).hostname not in {None, "localhost", "127.0.0.1", "0.0.0.0", "::1"}:
                target["url"] = f"{base}/?zone={z.id}"
            targets.append(target)
        groups = self._group_members(views)
        details = "\n\n".join(_render_event_details(v, groups.get(v.event.zone_id), approach_pct=self.cfg.approach_pct) for v in views)
        return Card(text, details, payload.image_path, targets, bool(self.charts_dir and not payload.image_path))

    async def dispatch(self, events: list[Event]) -> list[Delivery]:
        from .outbox import Card, fingerprint
        groups, ids = {}, []
        important_zones = {e.zone_id for e in events if e.kind not in self._QUIET and not e.delayed}
        for event in events:
            # Scanner returns its original dataclass after inserting the row;
            # that object can still have id=None. Resolve the persisted fact.
            if event.id is None:
                stored = self.db.conn.execute(
                    "SELECT id FROM event WHERE zone_id=? AND cycle_id=? AND kind=? AND occurred_at=?",
                    (event.zone_id, event.cycle_id, event.kind.value, event.occurred_at)).fetchone()
                if stored is None:
                    log.warning("Notification event is not in the journal: %s", event.idempotency_key(self.user))
                    continue
                event = self.db.get_event(stored["id"])
            view = self._load_view(event)
            if event.delayed or not self._passes_filters(view):
                continue
            delivery_id = self._record_pending(event)
            if delivery_id is None:
                continue
            ids.append(delivery_id)
            if self._entry_stale(view) or self._bot_blocked(view):
                self._finish_packet(event.id, "suppressed")
                continue
            quiet = event.kind in self._QUIET and event.zone_id not in important_zones
            ins = view.instrument.id if view.instrument else None
            tf = view.zone.timeframe if view.zone else ""
            groups.setdefault((ins, tf, quiet), []).append(view)
        for (iid, tf, quiet), views in groups.items():
            facts, fresh_views = [], []
            for v in views:
                e, z = v.event, v.zone
                # Terminal lifecycle events are unique per zone cycle regardless
                # of detector re-evaluation time or quote jitter.
                terminal = e.kind.value in {"fvg_filled", "ob_invalidated", "breaker_archived", "prb_archived", "level_taken"}
                fact = fingerprint([iid, tf, z.type.value if z else "", z.direction.value if z else "",
                              z.lower if z else None, z.upper if z else None,
                              z.formed_at if z else None, e.cycle_id, e.kind.value,
                              None if terminal else e.occurred_at])
                existing = self.db.conn.execute(
                    "SELECT p.semantic_key FROM notification_fact f JOIN notification_packet p ON p.id=f.packet_id "
                    "WHERE f.destination=? AND f.channel='htf' AND f.semantic_key=?",
                    (self.outbox.destination, fact)).fetchone()
                if existing:
                    self.outbox.put("htf", existing["semantic_key"], Card(""), [e.id], quiet=quiet)
                else:
                    previous = self.db.get_alert_state(e.zone_id, e.cycle_id, e.kind.value, self.user) if terminal else None
                    if previous and previous.last_delivered_at:
                        packet = self.outbox.put("htf", f"legacy-terminal:{fact}", Card(""), [e.id])
                        self.outbox.suppress(packet, "terminal fact delivered before outbox migration")
                        self._finish_packet(e.id, "suppressed")
                        continue
                    facts.append(fact)
                    fresh_views.append(v)
            if fresh_views:
                key = fingerprint([iid, tf, sorted(set(facts))])
                packet = self.outbox.put("htf", key, Card(""), [v.event.id for v in fresh_views], quiet=quiet)
                for fact in facts:
                    self.db.conn.execute(
                        "INSERT OR IGNORE INTO notification_fact(destination,channel,semantic_key,packet_id) VALUES(?,'htf',?,?)",
                        (self.outbox.destination, fact, packet))
                self.db.conn.commit()
            if not quiet:
                # Superseded digest entries stay in the journal, but never alert.
                for v in views:
                    pending = self.db.conn.execute(
                        "SELECT m.id,m.event_id FROM notification_packet p JOIN notification_member m ON m.packet_id=p.id "
                        "JOIN event e ON e.id=m.event_id WHERE p.channel='htf' AND p.quiet=1 "
                        "AND p.status IN ('pending','failed','bundled') AND e.zone_id=? AND e.cycle_id=? AND e.occurred_at<=?",
                        (v.event.zone_id, v.event.cycle_id, v.event.occurred_at)).fetchall()
                    for old in pending:
                        self.db.conn.execute("UPDATE notification_member SET reason='superseded' WHERE id=?", (old["id"],))
                        self._finish_packet(old["event_id"], "suppressed")
                    self.db.conn.commit()
        await self.outbox.flush()
        return self._deliveries_by_ids(ids)

    async def notify_service(self, text: str) -> None:
        self.services.note(text)
        await self.outbox.flush()

    async def retry_pending(self) -> list[Delivery]:
        # Adopt legacy unsent rows once; never touch successful history.
        for delivery in self.db.pending_deliveries():
            if None in delivery.event_ids:
                # Older deliveries could contain [null] for scanner objects.
                # The natural idempotency key still identifies the journal row.
                parts = delivery.idempotency_key.rsplit(":", 4)
                if len(parts) == 5:
                    stored = self.db.conn.execute(
                        "SELECT id FROM event WHERE zone_id=? AND cycle_id=? AND kind=? AND occurred_at=?",
                        tuple(parts[1:])).fetchone()
                    if stored:
                        import json
                        delivery.event_ids = [stored["id"]]
                        self.db.conn.execute("UPDATE delivery SET event_ids=? WHERE id=?",
                                             (json.dumps(delivery.event_ids), delivery.id))
                        self.db.conn.commit()
            for event_id in delivery.event_ids:
                exists = self.db.conn.execute(
                    "SELECT 1 FROM notification_member WHERE channel='htf' AND event_id=?", (event_id,)).fetchone()
                ev = self.db.get_event(event_id)
                if exists or ev is None:
                    continue
                if ev.delayed or self._entry_stale(self._load_view(ev)):
                    self._finish_packet(event_id, "suppressed")
                    continue
                from .outbox import Card
                self.outbox.put("htf", f"legacy:{event_id}", Card(""), [event_id], quiet=True)
        await self.outbox.flush()
        return self.db.pending_deliveries()

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
