"""LTF-уведомления (LTF-спека §11, §14): шаблоны, фильтры групп, дедуп
и ретрай доставки. Анализ и доставка разделены: фильтры не меняют журнал."""
from __future__ import annotations

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfEvent, LtfObservation, LtfScenario
from app.notify.ltf_queue import LtfDispatcher
from app.notify.ltf_templates import (
    TELEGRAM_TEXT_LIMIT,
    LtfContext,
    render_ltf_messages,
)
from tests.test_ltf_breaks import _series
from tests.test_ltf_engine import (
    SERIES_H_CLOSES,
    SERIES_H_HL,
    _setup as _engine_setup,
)

T0 = 1_780_000_000_000


class RecSender:
    """Отправитель-заглушка: журнал текстов (+ разметки кнопок send_ltf)
    и имитация сбоя транспорта."""

    def __init__(self):
        self.texts: list[str] = []
        self.markups: list = []
        self.photos: list[tuple[str, str, object]] = []
        self.fail = False

    async def send_card(self, card, packet_id, *, quiet=False):
        from app.notify.navigation import card_keyboard
        if card.image_path:
            await self.send_ltf_photo(card.image_path, card.text, card_keyboard(packet_id))
        else:
            await self.send_ltf(card.text, card_keyboard(packet_id))
        return len(self.texts) + len(self.photos)

    async def edit_card(self, message_id, card, packet_id, *, photo=False):
        if not photo:
            self.texts[message_id - 1] = card.text

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram недоступен (тест)")
        self.texts.append(text)

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        if self.fail:
            raise RuntimeError("telegram недоступен (тест)")
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def send_ltf_photo(self, image_path: str, caption: str,
                             reply_markup=None) -> None:
        if self.fail:
            raise RuntimeError("telegram недоступен (тест)")
        self.photos.append((image_path, caption, reply_markup))

    async def send(self, payload) -> None:  # Protocol Sender
        raise NotImplementedError


def _setup(db: Database, instrument_id: int):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    return db.get_zone(zid), obs, sc


def _ctx(db: Database, obs, sc) -> LtfContext:
    obs = db.get_ltf_observation(obs.id)
    return LtfContext(
        instrument=db.get_instrument(obs.instrument_id),
        zone=db.get_zone(obs.zone_id),
        observation=obs,
        scenario=db.get_ltf_scenario(sc.id) if sc else None,
    )


def _event(db: Database, obs, sc, kind: str, payload: dict,
           dedupe: str, delayed: bool = False) -> LtfEvent:
    ev, created = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id if sc else None,
        kind=kind, payload=payload, occurred_at=T0 + 1000, detected_at=T0 + 1000,
        dedupe_key=dedupe, delayed=delayed,
    ))
    assert created
    return ev


BOS_PAYLOAD = {
    "direction": "bear", "kind": "BOS", "stage": "primary",
    "break_level": 110.0, "break_candle_open_time": T0,
    "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0, "version": 1},
    "range_pending": False,
    "entries": [
        {"entry_zone_id": 1, "type": "FVG", "lower": 98.0, "upper": 102.0,
         "mid": 100.0, "overlap": "partial"},
        {"entry_zone_id": 2, "type": "OB", "lower": 105.0, "upper": 108.0,
         "mid": 106.5, "overlap": "full"},
        {"entry_zone_id": 3, "type": "BSL", "lower": 115.0, "upper": 115.0,
         "mid": 115.0, "overlap": "full"},
    ],
}


def test_render_bos_full_message(db: Database, instrument_id: int):
    zone, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:1:1")
    texts = render_ltf_messages(ev, _ctx(db, obs, sc))
    assert len(texts) == 1
    t = texts[0]
    assert "BTCUSDT · H1" in t
    assert "Подтверждён Bearish BOS (первичный)" in t
    assert "HTF-контекст: Orderblock D1 · медвежий" in t
    # §3.3: без ложного «внутри», если признак не передан
    assert "внутри" not in t
    assert "Уровень слома: 110,00" in t
    assert "Зоны входа:" in t
    assert "FVG H1: 98–102,00 (середина 100,00) — частично" in t
    assert "Orderblock H1: 105,00–108,00 (середина 106,50)" in t
    assert "BSL H1: 115,00" in t
    assert "Диапазон: 90–110,00" in t and "Середина: 100,00" in t
    # ТЗ 07.10.2026 §5.1: без «Источник»/TradingView-ссылки в тексте
    assert "Источник" not in t and "TradingView" not in t
    assert "Время события:" in t


def test_render_bos_range_pending_and_no_entries(db: Database, instrument_id: int):
    zone, obs, sc = _setup(db, instrument_id)
    # §11.2: слом есть, диапазон не готов
    ev = _event(db, obs, sc, "bos", {
        "direction": "bear", "kind": "BOS", "stage": "primary",
        "break_level": 110.0, "range_pending": True, "entries": [],
    }, "bos:1:2")
    t = render_ltf_messages(ev, _ctx(db, obs, sc))[0]
    assert "Ожидаем подтверждения LL/HH тремя свечами" in t
    assert "Зоны входа" not in t
    # диапазон готов, но свежих зон нет
    ev2 = _event(db, obs, sc, "sms", {
        "direction": "bear", "kind": "SMS", "stage": "primary",
        "break_level": 108.0,
        "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0, "version": 1},
        "range_pending": False, "entries": [],
    }, "sms:1:3")
    t2 = render_ltf_messages(ev2, _ctx(db, obs, sc))[0]
    assert "Bearish SMS" in t2
    assert "Подтверждение есть; подходящих зон входа сейчас нет" in t2


def test_render_bos_movement_line(db: Database, instrument_id: int):
    """Причинное движение слома (§8.1): строка в сообщении, когда движок
    положил movement в payload; без блока — как раньше, без строки."""
    zone, obs, sc = _setup(db, instrument_id)
    payload = {
        **BOS_PAYLOAD,
        "movement": {
            "start_price": 120.0, "end_price": 108.0,
            "start_at": T0 - 26 * 3_600_000, "end_at": T0,
            "candles": 26, "provenance_status": "ok",
        },
    }
    ev = _event(db, obs, sc, "bos", payload, "bos:1:mv")
    t = render_ltf_messages(ev, _ctx(db, obs, sc))[0]
    assert "Движение к слому: 120,00 → 108,00 (-10,00% · 26 свечей H1" in t
    assert "Происхождение неоднозначно" not in t
    # ambiguous-происхождение — явная пометка
    ev2 = _event(db, obs, sc, "bos", {
        **payload,
        "movement": {**payload["movement"], "provenance_status": "ambiguous"},
    }, "bos:1:mv2")
    t2 = render_ltf_messages(ev2, _ctx(db, obs, sc))[0]
    assert "Происхождение неоднозначно" in t2
    # movement is None (старые события) — строки нет
    t3 = render_ltf_messages(
        _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:1:mv3"),
        _ctx(db, obs, sc),
    )[0]
    assert "Движение к слому" not in t3


def test_render_fvg_parent_context(db: Database, instrument_id: int):
    """FVG-родитель (§16.1: htf_context_types=OB,FVG): события наблюдения на
    FVG-зоне рендерятся и доставляются по тому же конвейеру, тип — «FVG»."""
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=98.0, upper=102.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.WEAKENED,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BULL, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    ev = _event(db, obs, sc, "bos", {
        "direction": "bull", "kind": "BOS", "stage": "primary",
        "break_level": 105.0, "break_candle_open_time": T0,
        "range_pending": True, "entries": [], "htf_position": "inside",
    }, "bos:fvg:1")
    texts = render_ltf_messages(ev, _ctx(db, obs, sc))
    assert len(texts) == 1
    assert "Подтверждён Bullish BOS (первичный)" in texts[0]
    assert "HTF-контекст: FVG D1 · бычий" in texts[0]


def test_render_entries_ready_and_touch(db: Database, instrument_id: int):
    zone, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "entries_ready", {
        "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0, "version": 1},
        "entries": [{"entry_zone_id": 3, "type": "BSL", "lower": 115.0,
                     "upper": 115.0, "mid": 115.0, "overlap": "full"}],
    }, "entries_ready:1:3")
    t = render_ltf_messages(ev, _ctx(db, obs, sc))[0]
    assert "Новые Entry Zones по сценарию медвежий BOS" in t
    assert "BSL H1: 115,00" in t

    # §11.3: касание уровня — ожидание закрытия H1, без подтверждения входа
    touch = _event(db, obs, sc, "touch", {
        "entry_zone_id": 3, "type": "BSL", "lower": 115.0, "upper": 115.0,
        "mid": 115.0, "candle_open_time": T0,
    }, "touch:3:1")
    tt = render_ltf_messages(touch, _ctx(db, obs, sc))[0]
    assert "Цена коснулась Entry Zone: BSL H1" in tt
    assert "Уровень: 115,00" in tt
    assert "Основание: BOS" in tt
    assert "HTF-контекст: Orderblock D1 · медвежий" in tt
    assert "Ожидаем закрытия текущей H1 для проверки снятия без закрепления" in tt
    assert "Цена события" not in tt  # цены в payload нет — не выдумываем (§11)


def test_render_sweep_outcomes(db: Database, instrument_id: int):
    zone, obs, sc = _setup(db, instrument_id)
    base = {"entry_zone_id": 3, "type": "BSL", "level": 115.0,
            "close_price": 114.5, "candle_open_time": T0}
    ok = _event(db, obs, sc, "sweep_confirmed", dict(base, outcome="confirmed"),
                "sweep:3:a")
    t = render_ltf_messages(ok, _ctx(db, obs, sc))[0]
    assert "Ликвидность BSL снята." in t
    assert "Уровень: 115,00" in t and "Закрытие H1: 114,50" in t
    assert "повторные входы по этому уровню отключены" in t
    fail = _event(db, obs, sc, "sweep_failed", dict(base, outcome="failed"),
                  "sweep:3:b")
    t = render_ltf_messages(fail, _ctx(db, obs, sc))[0]
    assert "Ликвидность BSL снята с закреплением." in t
    eq = _event(db, obs, sc, "sweep_failed", dict(base, outcome="equal_close"),
                "sweep:3:c")
    t = render_ltf_messages(eq, _ctx(db, obs, sc))[0]
    assert "ровно на уровне" in t and "подтверждения снятия нет" in t


def test_render_cancellation(db: Database, instrument_id: int):
    zone, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "cancellation", {"reason": "reverse_bos"},
                "cancellation:1:x")
    t = render_ltf_messages(ev, _ctx(db, obs, sc))[0]
    assert "медвежий LTF-сценарий отменён" in t
    assert "Причина: обратный BOS H1" in t
    # HTF ещё валиден — ожидание нового подтверждения (§11.4)
    assert "ожидаем новое подтверждение" in t
    # структурная отмена ≠ закрытие позиции (§11.4)
    assert "позици" not in t

    manual = _event(db, obs, sc, "cancellation", {"reason": "manual"},
                    "cancellation:1:manual")
    t2 = render_ltf_messages(manual, _ctx(db, obs, sc))[0]
    assert "Причина: ручное завершение" in t2
    assert "ожидаем новое подтверждение" not in t2


def test_render_splits_long_entry_list(db: Database, instrument_id: int):
    """§11.1: длинный список зон — несколько сообщений, ни одна зона
    не пропущена."""
    zone, obs, sc = _setup(db, instrument_id)
    entries = [
        {"entry_zone_id": i, "type": "FVG", "lower": 100.0 + i,
         "upper": 101.0 + i, "mid": 100.5 + i, "overlap": "full"}
        for i in range(200)
    ]
    ev = _event(db, obs, sc, "bos", dict(BOS_PAYLOAD, entries=entries), "bos:1:many")
    texts = render_ltf_messages(ev, _ctx(db, obs, sc))
    assert len(texts) > 1
    assert all(len(t) <= TELEGRAM_TEXT_LIMIT + 200 for t in texts)  # head/футер
    delivered_ids = []
    for t in texts:
        for line in t.splitlines():
            if line.startswith("FVG H1:"):
                delivered_ids.append(line)
    assert len(delivered_ids) == 200  # все зоны доставлены, без пропусков
    assert len(set(delivered_ids)) == 200
    assert "Зоны входа:" in texts[0]
    assert "Время события:" in texts[-1]  # время — в завершении списка


# ---------- диспетчер: фильтры, дедуп, ретрай ----------

def _dispatcher(db, sender, kinds=None):
    cfg = DetectorConfig()
    if kinds is not None:
        cfg.ltf_notify_kinds = kinds
    return LtfDispatcher(db, cfg, sender)


async def test_dispatcher_sends_and_marks_delivered(db: Database, instrument_id: int):
    zone, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:d:1")
    sender = RecSender()
    disp = _dispatcher(db, sender)
    await disp([ev])
    assert len(sender.texts) == 1
    assert "BOS подтверждён" in sender.texts[0]
    assert db.get_ltf_event(ev.id).delivered is True
    # повторный вызов с тем же событием — без повторной отправки (§11.5)
    await disp([ev])
    assert len(sender.texts) == 1


async def test_dispatcher_notify_kinds_filter(db: Database, instrument_id: int):
    """§14: выключенная группа не доставляется; анализ (журнал) не меняется."""
    zone, obs, sc = _setup(db, instrument_id)
    bos = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:f:1")
    touch = _event(db, obs, sc, "touch", {
        "entry_zone_id": 3, "type": "BSL", "lower": 115.0, "upper": 115.0,
        "mid": 115.0, "candle_open_time": T0,
    }, "touch:f:1")
    sender = RecSender()
    disp = _dispatcher(db, sender, kinds="bos_sms,cancellation")
    await disp([bos, touch])
    assert len(sender.texts) == 1
    assert "BOS подтверждён" in sender.texts[0]
    # событие touch осталось в журнале недоставленным — анализ не тронут
    assert db.get_ltf_event(touch.id).delivered is True  # disabled: no backlog after enabling
    assert db.get_ltf_event(bos.id).delivered is True


async def test_dispatcher_skips_delayed(db: Database, instrument_id: int):
    """§13: восстановленные replay-события не доставляются как текущие."""
    zone, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:dl:1", delayed=True)
    sender = RecSender()
    disp = _dispatcher(db, sender)
    await disp([ev])
    await disp.retry_pending()
    assert sender.texts == []
    assert db.get_ltf_event(ev.id).delivered is False


async def test_dispatcher_retry_after_transport_failure(db: Database, instrument_id: int):
    """§9: отказ Telegram не отменяет факт события — ретрай доставки."""
    zone, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:r:1")
    sender = RecSender()
    disp = _dispatcher(db, sender)
    sender.fail = True
    await disp([ev])
    assert sender.texts == []
    assert db.get_ltf_event(ev.id).delivered is False
    # транспорт восстановлен — retry_pending подбирает недоставленное
    sender.fail = False
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    await disp.retry_pending()
    assert len(sender.texts) == 1
    assert db.get_ltf_event(ev.id).delivered is True
    # следующий ретрай не шлёт повторно
    await disp.retry_pending()
    assert len(sender.texts) == 1


async def test_dispatcher_touch_not_resent_after_range_recalc(db: Database, instrument_id: int):
    """§11.5: пересчёт диапазона не оживляет касание — диспетчерская часть:
    событие touch с доставленным статусом не уходит повторно ни при каких
    повторных вызовах."""
    zone, obs, sc = _setup(db, instrument_id)
    touch = _event(db, obs, sc, "touch", {
        "entry_zone_id": 3, "type": "FVG", "lower": 98.0, "upper": 102.0,
        "mid": 100.0, "candle_open_time": T0,
    }, "touch:3:1")
    sender = RecSender()
    disp = _dispatcher(db, sender)
    await disp([touch])
    # «пересчёт range_version» в БД события не создаёт — ключ без версии (§11.5);
    # повторная попытка доставки того же события отсекается по delivered
    await disp([touch])
    await disp.retry_pending()
    assert len(sender.texts) == 1
    t = sender.texts[0]
    assert "Цена коснулась зоны входа" in t
    assert "Ожидаем закрытия" not in t  # OB/FVG — без строки ожидания (§11.3)


# --- F01/A01: live-события внутри grace-окна доставляются --------------------


async def _deliver_bos_with_lag(db: Database, instrument_id: int,
                                sender: RecSender, lag_ms: int) -> list:
    """Серия H до первичного BOS (idx14); свеча слома обнаружена с лагом
    lag_ms после закрытия; новые события движка уходят в диспетчер."""
    engine = LtfEngine(db, DetectorConfig())
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _engine_setup(db, instrument_id)
    engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), occurred_at=T0)
    for c in candles[:14]:
        db.insert_candles([c])
        engine.process_h1_close(instrument_id, now_ms=c.close_time)
    c = candles[14]
    db.insert_candles([c])
    result = engine.process_h1_close(instrument_id,
                                     now_ms=c.close_time + lag_ms)
    disp = _dispatcher(db, sender)
    await disp(result.events)
    return result.events


@pytest.mark.parametrize("lag_ms", [1_000, 60_000])
async def test_a01_fresh_bos_within_grace_delivered_once(
        db: Database, instrument_id: int, lag_ms: int):
    """A01: свежий BOS, обнаруженный внутри grace-окна (1 с / 60 с),
    доставляется ровно один раз. До фикса любой лаг опроса делал событие
    delayed, и диспетчер с pending_ltf_events навсегда его подавляли."""
    sender = RecSender()
    events = await _deliver_bos_with_lag(db, instrument_id, sender, lag_ms)
    assert len(sender.texts) == 1
    assert "BOS подтверждён" in sender.texts[0]
    bos = [e for e in events if e.kind == "bos"][0]
    assert db.get_ltf_event(bos.id).delivered is True
    # повторные вызовы и ретрай не шлют второй раз (§11.5)
    disp = _dispatcher(db, sender)
    await disp(events)
    await disp.retry_pending()
    assert len(sender.texts) == 1


async def test_a01_catchup_bos_not_delivered(db: Database, instrument_id: int):
    """A01: лаг дольше grace-окна — catchup: диспетчер и ретрай подавляют
    (семантика delayed сохранена, gates не менялись)."""
    sender = RecSender()
    events = await _deliver_bos_with_lag(db, instrument_id, sender, 901_000)
    disp = _dispatcher(db, sender)
    await disp(events)
    await disp.retry_pending()
    assert sender.texts == []
    bos = [e for e in events if e.kind == "bos"][0]
    assert db.get_ltf_event(bos.id).delivered is False
