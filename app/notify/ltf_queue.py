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
        end = ev.occurred_at + 3_600_000  # закрытие свечи события + шаг H1
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

    async def _send_event(self, ev: LtfEvent, ctx: LtfContext,
                          texts: list[str], markup) -> None:
        """Отправка одного события: фото с подписью, когда график готов и
        текст помещается в caption; иначе текст (+ фото отдельным сообщением
        при длинном тексте). Исключение транспорта пробрасывается — ретрай."""
        chart_path: Optional[str] = None
        if ev.kind in _CHART_KINDS:
            try:
                chart_path = await self._render_event_chart(ev, ctx)
            except Exception:  # noqa: BLE001 — ошибка рендера ≠ ошибка события
                log.warning("LTF: не удалось сгенерировать график события %s",
                            ev.id, exc_info=True)
        chart_failed = ev.kind in _CHART_KINDS and chart_path is None
        if chart_failed:
            # §7: текст без изображения — НЕ успешная доставка графика;
            # картинку дошлём retry_charts без повторного уведомления
            texts = list(texts)
            texts[-1] += "\n📊 График временно недоступен — пришлём отдельно."

        if (
            chart_path is not None
            and len(texts) == 1
            and len(texts[0]) <= _CAPTION_LIMIT
        ):
            await self.sender.send_ltf_photo(
                chart_path, caption=texts[0], reply_markup=markup
            )
        else:
            if chart_path is not None:
                head = texts[0].split("\n", 1)[0]
                await self.sender.send_ltf_photo(
                    chart_path,
                    caption=(
                        f"📊 {head} · снимок {fmt_time_msk(ev.occurred_at)}"
                    ),
                    reply_markup=None,
                )
            for i, text in enumerate(texts):
                # кнопки — на завершающей части (там футер со временем)
                await self.sender.send_ltf(
                    text,
                    reply_markup=markup if i == len(texts) - 1 else None,
                )
        # состояние графика — только после успешной отправки (§7)
        if ev.id is not None and ev.kind in _CHART_KINDS:
            self.db.update_ltf_event_chart(
                ev.id, "failed" if chart_failed else "sent",
                bump_attempts=chart_failed,
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
            if self._event_stale(ev):
                # §13.6: просроченный вход не уходит и не копится к ретраю —
                # событие остаётся фактом в журнале, доставка закрывается
                log.info("LTF: доставка %s пропущена — вход просрочен (§13.6)",
                         ev.kind)
                self.db.mark_ltf_event_delivered(ev.id)
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
                await self._send_event(ev, ctx, texts, markup)
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
        """§7 ТЗ 07.10.2026: повторная генерация графиков к уже доставленным
        событиям — досылает картинку без повторного рыночного уведомления."""
        sent = 0
        for ev in self.db.pending_ltf_charts():
            ctx = self._load_context(ev)
            try:
                path = await self._render_event_chart(ev, ctx)
            except Exception:  # noqa: BLE001
                path = None
            if path is None:
                self.db.update_ltf_event_chart(ev.id, "failed",
                                               bump_attempts=True)
                continue
            markup = self._keyboard(ev, ctx)
            caption = f"📊 График к событию от {fmt_time_msk(ev.occurred_at)}"
            try:
                await self.sender.send_ltf_photo(
                    path, caption=caption, reply_markup=markup
                )
            except Exception:  # noqa: BLE001
                self.db.update_ltf_event_chart(ev.id, "failed",
                                               bump_attempts=True)
                continue
            self.db.update_ltf_event_chart(ev.id, "sent")
            sent += 1
        return sent

    async def __call__(self, events: list[LtfEvent]) -> None:
        await self.deliver(events)
