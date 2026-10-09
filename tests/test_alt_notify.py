"""Telegram-доставка модуля «Altcoins D1 accumulation» (ТЗ 07.10.2026 §18,
§2, §12).

Покрытие: рендер каждого типа события (ключевые фразы, ссылка на график от
публичного URL, МСК-время, источник, as_of), запрет термина «стоп-лосс» и
формулировка §12 при K<=0, оговорка о неизвестной внутрисвечной
последовательности; доставка outbox (delivered/ретрай/дедуп), объединение
событий одного сетапа одного run (несколько TP — одно сообщение,
терминальный приоритет над входными), фильтр группы «alt» настроек бота,
разовая сводка первичной загрузки.
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.db import Database
from app.models_alt import (
    AltAsset,
    AltEvent,
    AltFrozenRange,
    AltInstrumentSource,
    AltRangeCandidate,
    AltRun,
    AltSetup,
)
from app.notify.alt_queue import AltDispatcher
from app.notify.alt_templates import render_alt_message, render_backfill_summary

DAY = 86_400_000
T0 = 1_800_000_000_000  # произвольная опора времени (ms UTC)
BASE = "https://htf.example.com"
CHAT = "42"


class FakeSender:
    """Журнал отправок; fail=True — транспорт падает (ретрай)."""

    def __init__(self, fail: bool = False):
        self.alt: list[str] = []
        self.texts: list[str] = []
        self.fail = fail

    async def send_card(self, card, packet_id, *, quiet=False):
        if self.fail:
            raise RuntimeError("transport unavailable")
        if not hasattr(self, "details"):
            self.details = []
        self.details.append(card.details)
        if card.text.startswith("🔕"):
            self.texts.append(card.text)
        else:
            self.alt.append(card.text)
        return len(self.alt) + len(self.texts)

    async def edit_card(self, message_id, card, packet_id, *, photo=False):
        self.alt[message_id - 1] = card.text

    async def send_alt(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.alt.append(text)

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.texts.append(text)


def _settings(public: bool = True, chat_id: str = "") -> Settings:
    s = Settings()
    if public:
        s.public_base_url = BASE
    s.telegram_chat_id = chat_id
    return s


def _seed(db: Database, k=0.5, cancel_reachable=True, targets=None,
          universe_eligible=True):
    """Актив/источник/кандидат/frozen/сетап — минимум для контекста."""
    asset = db.upsert_alt_asset(AltAsset(
        id=None, cmc_id=777, symbol="PUMP", name="Pump", cmc_rank=42,
    ))
    src = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue="bybit", symbol="PUMPUSDT",
    ))
    cand = db.insert_alt_range_candidate(AltRangeCandidate(
        id=None, asset_id=asset.id, origin_key="o1",
        start_anchor_open_time=T0, rebound_anchor_open_time=T0 + DAY,
        lower=1.0, upper=2.0, width=1.0, mid=1.5, n_days=120,
    ))
    frozen = db.insert_alt_frozen_range(AltFrozenRange(
        id=None, range_id=cand.id, lower=1.0, upper=2.0, width=1.0, mid=1.5,
        start_anchor_open_time=T0, rebound_anchor_open_time=T0 + DAY,
        included_candles=120, mature_at_ms=T0 + 120 * DAY,
    ))
    setup, _ = db.insert_alt_setup(AltSetup(
        id=None, asset_id=asset.id, source_id=src.id, range_id=frozen.id,
        state="active_confirmed",
        targets_json=json.dumps(targets or []),
        cancel_price=k, cancel_mode="wick_on_closed_d1",
        cancel_reachable=cancel_reachable,
        universe_eligible=universe_eligible,
    ))
    return asset, src, frozen, setup


_n = 0


def _event(db: Database, setup_id: int, etype: str, payload: dict,
           run_id=None, event_time=T0, detected=T0 + 1000) -> AltEvent:
    global _n
    _n += 1
    ev, created = db.insert_alt_event(AltEvent(
        id=None, setup_id=setup_id, event_type=etype,
        source_event_id=f"{etype}:{setup_id}:{_n}",
        payload_json=json.dumps(payload, ensure_ascii=False),
        event_time_ms=event_time, detected_at_ms=detected, run_id=run_id,
    ))
    assert created
    return ev


def _run(db: Database) -> AltRun:
    return db.insert_alt_run(AltRun(
        id=None, started_ms=T0, as_of_ms=T0, status="ok",
    ))


def _ctx(disp: AltDispatcher, ev: AltEvent):
    return disp._load_context(ev)


TARGETS = [
    {"tp": n, "price": 2.0 + n, "passed_at_confirmation": n == 1}
    for n in range(1, 5)
]


# ---------------------------------------------------------------------------
# Рендер отдельных типов (§18)
# ---------------------------------------------------------------------------


def test_render_forming_started(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    run = _run(db)
    ev = _event(db, 1, "forming_started", {
        "n_days_at_recognition": 62, "drawdown_pct": 82.75,
        "lower": 1.0, "upper": 2.0,
    }, run_id=run.id)
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "PUMP · D1" in text
    assert "Найдена аккумуляция 62 дней" in text
    assert "82,75% от биржевого ATH" in text
    assert "Диапазон 1–2. Формируется." in text
    assert f"График: {BASE}/alt.html?asset=777" in text
    assert "Источник: bybit spot / PUMPUSDT" in text
    assert "as_of:" in text and "Сетап #1" in text
    assert "МСК" in text


def test_render_forming_started_legacy_payload(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "forming_started", {"n_days_at_recognition": 62})
    text = render_alt_message([ev], _ctx(disp, ev))
    # старые события без drawdown — порог допуска модуля, не выдуманный %
    assert "после падения более 80% от биржевого ATH" in text


def test_render_mature_frozen(db):
    _seed(db, k=0.5)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "mature_frozen", {
        "lower": 1.0, "upper": 2.0, "mid": 1.5, "included_candles": 120,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Зрелый диапазон зафиксирован: 1–2 (середина 1,5)" in text
    assert "120 дн" in text
    assert "Уровень отмены K = 0,5" in text
    assert "проектная настройка v1" in text
    assert "стоп" not in text.lower()


def test_render_mature_frozen_k_unreachable(db):
    _seed(db, k=-0.2, cancel_reachable=False)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "mature_frozen", {
        "lower": 1.0, "upper": 2.2, "mid": 1.6, "included_candles": 120,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    # §12: K не обрезается и не подменяется — точная проектная формулировка
    assert "По выбранной формуле ценовой уровень отмены неположительный" in text
    assert "стоп" not in text.lower()


def test_render_manipulation(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev1 = _event(db, 1, "manipulation_started", {"range_lower": 1.0, "low": 0.9})
    text = render_alt_message([ev1], _ctx(disp, ev1))
    assert "Нижний вынос" in text and "0,9" in text
    assert "не отмена сетапа" in text
    ev2 = _event(db, 1, "manipulation_ended", {
        "min_price": 0.85, "days_below": 5,
    })
    text = render_alt_message([ev2], _ctx(disp, ev2))
    assert "Возврат выше 1" in text
    assert "Дней ниже границы: 5" in text and "0,85" in text


def test_render_ssl_taken(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Снятие внутреннего SSL: опора 1,1, закрытие D1 1,2" in text


def test_render_bos_entry_a_combined(db):
    _seed(db, targets=TARGETS)
    disp = AltDispatcher(db, _settings(), FakeSender())
    run = _run(db)
    bos = _event(db, 1, "bos_confirmed", {
        "level_price": 1.8, "close": 1.9,
    }, run_id=run.id)
    ea = _event(db, 1, "entry_a", {"price": 1.9, "bases": ["bos"]},
                run_id=run.id)
    text = render_alt_message([bos, ea], _ctx(disp, bos))
    assert "Подтверждён bullish BOS: уровень слома 1,8, закрытие D1 1,9" in text
    assert "Возможность A по закрытию 1,9" in text
    assert "TP1 — 3 (пройдена к моменту подтверждения)" in text
    assert "TP4 — 6" in text


def test_render_breakout(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "breakout", {
        "close": 2.1, "upper": 2.0, "closed_at": T0,
        "retest_deadline_ms": T0 + 14 * DAY,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Выход выше 2: закрытие D1 2,1" in text
    assert "Ждём ретест 1,5–2 до" in text


def test_render_retest_entry_b(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    run = _run(db)
    rt = _event(db, 1, "retest", {
        "zone": {"lower": 1.5, "upper": 2.0}, "candle_open_time": T0,
    }, run_id=run.id)
    eb = _event(db, 1, "entry_b", {"zone": {"lower": 1.5, "upper": 2.0}},
                run_id=run.id)
    text = render_alt_message([rt, eb], _ctx(disp, rt))
    assert "Касание ретеста 1,5–2" in text
    assert "Дополнительная возможность B того же сетапа" in text
    assert "Дневная свеча" in text


def test_render_target_hit_multiple_levels(db):
    _seed(db, targets=TARGETS)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "target_hit", {
        "levels": [1, 2], "prices": {"1": 3.0, "2": 4.0},
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Достигнуты цели:" in text
    assert "TP1 — 3" in text and "TP2 — 4" in text


def test_render_cancelled_no_forbidden_term(db):
    _seed(db, k=0.5)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "cancelled", {
        "cancel_price": 0.5, "cancel_mode": "wick_on_closed_d1",
        "cancel_mode_note": "project_default_v1", "candle_open_time": T0,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Сетап отменён" in text
    assert "K = 0,5" in text and "K = 2L − U" in text
    assert "тень закрытой D1 (Low ≤ K)" in text
    assert "проектная настройка v1" in text
    # §12: термин «стоп-лосс» не используется нигде
    assert "стоп" not in text.lower()


def test_render_expired_and_completed_and_review(db):
    _seed(db, targets=TARGETS)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "expired_no_retest", {
        "first_breakout_closed_at": T0, "deadline": T0 + 14 * DAY,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Сетап завершён: за 14 дней после выхода ретеста не было" in text
    assert "не команда закрывать позицию" in text

    ev = _event(db, 1, "targets_completed", {"levels": [1, 2, 3, 4]})
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Все 4 цели (TP1–TP4) достигнуты" in text
    assert "аналитический план уровней завершён" in text

    ev = _event(db, 1, "review_required", {
        "chosen_start_anchor": T0, "alternative_anchors": [T0 + DAY],
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Требуется проверка опор" in text
    assert "Альтернативные опоры" in text


def test_render_intra_candle_caveat(db):
    _seed(db)
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "target_hit", {
        "levels": [1], "prices": {"1": 3.0},
        "intra_candle_sequence_unknown": True,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "Порядок событий внутри дневной свечи неизвестен" in text


def test_render_terminal_priority_over_entries(db):
    _seed(db, k=0.5, targets=TARGETS)
    disp = AltDispatcher(db, _settings(), FakeSender())
    run = _run(db)
    ea = _event(db, 1, "entry_a", {"price": 1.9, "bases": ["bos"]},
                run_id=run.id)
    cancel = _event(db, 1, "cancelled", {
        "cancel_price": 0.5, "cancel_mode": "wick_on_closed_d1",
    }, run_id=run.id)
    text = render_alt_message([ea, cancel], _ctx(disp, ea))
    # терминальное ведёт сообщение, вход — демонтированная контекстная строка
    assert text.index("Сетап отменён") < text.index("Контекст")
    assert "была возможность A по закрытию 1,9" in text
    assert "стоп" not in text.lower()


def test_render_link_only_public_url(db):
    _seed(db)
    payload = {"n_days_at_recognition": 62}
    disp = AltDispatcher(db, _settings(public=True), FakeSender())
    ev = _event(db, 1, "forming_started", payload)
    text = render_alt_message([ev], _ctx(disp, ev))
    assert f"{BASE}/alt.html?asset=777" in text
    # localhost/127.0.0.1 в ссылке не отправляем (§18)
    disp_local = AltDispatcher(db, _settings(public=False), FakeSender())
    text = render_alt_message([ev], _ctx(disp_local, ev))
    assert "График:" not in text
    assert "127.0.0.1" not in text and "localhost" not in text


def test_render_small_price_precision(db):
    """Мелкие альткоин-цены — до 8 знаков, хвостовые нули обрезаны."""
    asset = db.upsert_alt_asset(AltAsset(
        id=None, cmc_id=778, symbol="SHIB", name="Shiba", cmc_rank=15,
    ))
    src = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue="bybit", symbol="SHIBUSDT",
    ))
    cand = db.insert_alt_range_candidate(AltRangeCandidate(
        id=None, asset_id=asset.id, origin_key="o2",
        start_anchor_open_time=T0, rebound_anchor_open_time=T0 + DAY,
        lower=0.00001234, upper=0.00002468, width=0.00001234,
        mid=0.00001851, n_days=120,
    ))
    frozen = db.insert_alt_frozen_range(AltFrozenRange(
        id=None, range_id=cand.id, lower=0.00001234, upper=0.00002468,
        width=0.00001234, mid=0.00001851,
        start_anchor_open_time=T0, rebound_anchor_open_time=T0 + DAY,
        included_candles=120, mature_at_ms=T0,
    ))
    db.insert_alt_setup(AltSetup(
        id=None, asset_id=asset.id, source_id=src.id, range_id=frozen.id,
        cancel_price=0.00000123, cancel_reachable=True,
    ))
    disp = AltDispatcher(db, _settings(), FakeSender())
    ev = _event(db, 1, "mature_frozen", {
        "lower": 0.00001234, "upper": 0.00002468, "mid": 0.00001851,
    })
    text = render_alt_message([ev], _ctx(disp, ev))
    assert "0,00001234–0,00002468" in text


# ---------------------------------------------------------------------------
# Доставка outbox
# ---------------------------------------------------------------------------


async def test_dispatch_marks_delivered_and_dedup(db):
    _seed(db)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(), sender)
    ev = _event(db, 1, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    sent = await disp.dispatch_pending()
    assert sent == 1 and len(sender.alt) == 1
    assert "Снятие внутреннего SSL" in sender.alt[0]
    assert db.pending_alt_events() == []
    # повторный вызов — ничего не отправляет (идемпотентность)
    assert await disp.dispatch_pending() == 0
    assert len(sender.alt) == 1
    assert ev.id is not None


async def test_disabled_alt_asset_is_not_sent(db):
    """Выключенный альткоин не получает карточку и не остаётся в очереди."""
    asset, _src, _frozen, setup = _seed(db)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(), sender)
    _event(db, setup.id, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    asset.enabled = False
    db.upsert_alt_asset(asset)
    assert await disp.dispatch_pending() == 0
    assert sender.alt == []
    assert db.pending_alt_events() == []


async def test_queued_alt_is_dropped_when_asset_turns_off(db):
    """Пакет, не ушедший до выключения альткоина, после выключения не уходит."""
    asset, _src, _frozen, setup = _seed(db)
    sender = FakeSender(fail=True)
    disp = AltDispatcher(db, _settings(), sender)
    _event(db, setup.id, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    assert await disp.dispatch_pending() == 0
    asset.enabled = False
    db.upsert_alt_asset(asset)
    sender.fail = False
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    assert await disp.dispatch_pending() == 0
    assert sender.alt == []
    assert db.pending_alt_events() == []


async def test_dispatch_failure_leaves_pending_then_retry(db):
    _seed(db)
    sender = FakeSender(fail=True)
    disp = AltDispatcher(db, _settings(), sender)
    _event(db, 1, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    assert await disp.dispatch_pending() == 0
    assert len(db.pending_alt_events()) == 1  # не доставлено — ретрай
    sender.fail = False
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    assert await disp.retry_pending() == 1
    assert db.pending_alt_events() == []
    assert len(sender.alt) == 1


async def test_dispatch_combines_same_setup_same_run(db):
    _seed(db, targets=TARGETS)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(), sender)
    run = _run(db)
    _event(db, 1, "bos_confirmed", {"level_price": 1.8, "close": 1.9},
           run_id=run.id)
    _event(db, 1, "entry_a", {"price": 1.9, "bases": ["bos"]}, run_id=run.id)
    _event(db, 1, "target_hit", {
        "levels": [1, 2], "prices": {"1": 3.0, "2": 4.0},
    }, run_id=run.id)
    sent = await disp.dispatch_pending()
    assert sent == 3
    # три события одного сетапа одного run — ОДНО сообщение
    assert len(sender.alt) == 1
    text = sender.details[0]
    assert "Подтверждён bullish BOS" in text
    assert "Возможность A" in text
    assert "Достигнуты цели:" in text


async def test_dispatch_separate_runs_separate_messages(db):
    _seed(db)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(), sender)
    run1, run2 = _run(db), _run(db)
    _event(db, 1, "manipulation_started", {"range_lower": 1.0, "low": 0.9},
           run_id=run1.id)
    _event(db, 1, "manipulation_ended", {"min_price": 0.9, "days_below": 2},
           run_id=run2.id)
    assert await disp.dispatch_pending() == 2
    assert len(sender.alt) == 2


async def test_dispatch_terminal_priority_in_one_batch(db):
    _seed(db, k=0.5, targets=TARGETS)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(), sender)
    run = _run(db)
    _event(db, 1, "entry_a", {"price": 1.9, "bases": ["bos"]}, run_id=run.id)
    _event(db, 1, "cancelled", {
        "cancel_price": 0.5, "cancel_mode": "wick_on_closed_d1",
    }, run_id=run.id)
    assert await disp.dispatch_pending() == 2
    assert len(sender.alt) == 1
    text = sender.details[0]
    assert text.index("Сетап отменён") < text.index("Контекст")


async def test_dispatch_group_filter_blocks_kind(db):
    """ТЗ п.9: выключение вида в группе «alt» останавливает только доставку
    этого вида; подавленное помечается доставленным — после включения
    накопившееся не уходит."""
    _seed(db, targets=TARGETS)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(chat_id=CHAT), sender)
    run = _run(db)
    _event(db, 1, "bos_confirmed", {"level_price": 1.8, "close": 1.9},
           run_id=run.id)
    _event(db, 1, "entry_a", {"price": 1.9, "bases": ["bos"]}, run_id=run.id)
    db.set_alert_pref(CHAT, "global", "", "alt", "entry_a", False)
    sent = await disp.dispatch_pending()
    assert sent == 1  # доставлен только BOS
    assert len(sender.alt) == 1
    assert "Подтверждён bullish BOS" in sender.alt[0]
    assert "Возможность A" not in sender.alt[0]
    assert db.pending_alt_events() == []  # подавленное тоже закрыто


async def test_dispatch_group_filter_blocks_all(db):
    _seed(db)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(chat_id=CHAT), sender)
    _event(db, 1, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    db.set_alert_pref(CHAT, "global", "", "alt", "ssl_taken", False)
    assert await disp.dispatch_pending() == 0
    assert sender.alt == []
    assert db.pending_alt_events() == []


# ---------------------------------------------------------------------------
# Сводка первичной загрузки (§18)
# ---------------------------------------------------------------------------


def test_render_backfill_summary():
    summary = {
        "processed": 250, "errors": 2, "universe_stale": False,
        "per_asset": [
            {"status": "processed"},
            {"status": "skipped", "reason": "no_spot_pair"},
            {"status": "skipped", "reason": "no_spot_pair"},
            {"status": "error", "reason": "boom"},
        ],
    }
    text = render_backfill_summary(
        summary, {"forming": 3, "mature": 2, "active_confirmed": 1},
    )
    assert "первичная загрузка завершена" in text
    assert "Обработано монет: 250" in text
    assert "без спотовой пары: 2" in text
    assert "Найдено сетапов: 6" in text
    assert "формируются: 3" in text and "зрелые: 2" in text
    assert "Исторические события не рассылаются" in text


async def test_backfill_summary_fires_once(db):
    _seed(db)
    sender = FakeSender()
    disp = AltDispatcher(db, _settings(), sender)
    summary = {"processed": 1, "errors": 0, "per_asset": [],
               "backfill_event_ids": [1]}
    assert await disp.notify_backfill_summary(summary) is True
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    await disp.outbox.flush()
    assert len(sender.texts) == 1
    assert "первичная загрузка завершена" in sender.texts[0]
    # повтор — молчим: meta-флаг после успешной отправки
    assert await disp.notify_backfill_summary(summary) is False
    assert len(sender.texts) == 1
    # сбой транспорта — флаг не ставится, сводку можно повторить
    db2 = Database(":memory:")
    try:
        _seed(db2)
        failing = FakeSender(fail=True)
        disp2 = AltDispatcher(db2, _settings(), failing)
        assert await disp2.notify_backfill_summary(summary) is True  # durable enqueue before transport
        failing.fail = False
        assert await disp2.notify_backfill_summary(summary) is False
        db2.conn.execute("UPDATE notification_packet SET due_at=0")
        db2.conn.commit()
        await disp2.outbox.flush()
        assert len(failing.texts) == 1
    finally:
        db2.close()
