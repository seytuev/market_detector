"""Подавление повторных уведомлений (§8 спеки).

Правила:
- ключ подавления: user + zone_id + cycle_id + вид события/порог (AlertState);
- повтор на той же глубине молчит cfg.suppress_hours (120 ч = 5 дней) с момента
  УСПЕШНОЙ доставки предыдущего уведомления, а не с момента события;
- новый порог (DEPTH_50 после недавнего TOUCH) не блокируется — ключи разные;
- события вне SUPPRESSED_KINDS (DEPTH_90, BREAKER_CREATED и т.д.) не подавляются;
- muted_until («Отложить»/«Отключить», §9) глушит зону целиком до срока;
- само истечение срока не создаёт событие; ежедневных напоминаний нет (§8);
- перезапуск процесса не сбрасывает сроки — состояние в SQLite (§13.12).
"""
from __future__ import annotations

from typing import Optional

from ..config import DetectorConfig
from ..db import Database
from ..models import SUPPRESSED_KINDS, AlertState, Event, EventKind, now_ms

# ТЗ бота п.9: группы настроек уведомлений (/alerts). Вид события → группа:
# HTF-события — «htf»; LTF — касание Entry Zone в «entry», отмена сценария
# в «scenario», остальные в «ltf»; сервисные — «service».
BOT_GRP_HTF = "htf"
BOT_GRP_LTF = "ltf"
BOT_GRP_ENTRY = "entry"
BOT_GRP_SCENARIO = "scenario"
BOT_GRP_SERVICE = "service"
BOT_GRP_ALT = "alt"

BOT_ALERT_GROUPS: dict[str, list[str]] = {
    BOT_GRP_HTF: [k.value for k in EventKind],
    BOT_GRP_LTF: ["bos", "sms", "entries_ready", "range_ready",
                  "sweep_confirmed", "sweep_failed"],
    BOT_GRP_ENTRY: ["touch"],
    BOT_GRP_SCENARIO: ["cancellation"],
    BOT_GRP_SERVICE: ["service"],
    # Отдельная подписка «Альткоины D1» (ТЗ 07.10.2026 §18): по умолчанию
    # все события модуля — формирование, зрелость, манипуляция, SSL,
    # BOS/SMS, breakout, ретест, входы A/B, цели, отмена, истечение,
    # завершение целей, проверка опор
    BOT_GRP_ALT: [
        "forming_started", "mature_frozen",
        "manipulation_started", "manipulation_ended",
        "ssl_taken", "bos_confirmed", "sms_confirmed",
        "breakout", "retest", "entry_a", "entry_b",
        "target_hit", "cancelled", "expired_no_retest",
        "targets_completed", "review_required",
    ],
}


def bot_group_for_ltf_kind(kind: str) -> str:
    if kind == "touch":
        return BOT_GRP_ENTRY
    if kind == "cancellation":
        return BOT_GRP_SCENARIO
    return BOT_GRP_LTF


def bot_group_for_alt_kind(kind: str) -> str:
    """Все события модуля альткоинов — в одной группе подписки (§18)."""
    return BOT_GRP_ALT


def instrument_off(db: Database, instrument_id: Optional[int]) -> bool:
    """Выключенный актив не получает рыночные уведомления.

    ``enabled=0`` — выключатель актива и прежний toggle. Строка без
    инструмента не глушится: у старого события может не быть привязки.
    """
    if instrument_id is None:
        return False
    ins = db.get_instrument(int(instrument_id))
    return ins is not None and not ins.enabled


def bot_delivery_blocked(
    db: Database,
    chat_id: Optional[str],
    *,
    grp: str,
    kind: str,
    instrument_id: Optional[int] = None,
    zone_id: Optional[int] = None,
    now: Optional[int] = None,
) -> bool:
    """True — доставку подавить настройками бота (ТЗ п.9): bot_mute
    (all/instrument/zone), bot_alert_pref (global → instrument → context),
    alerts_enabled=0 в watchlist. Пустые таблицы или chat_id=None — старое
    поведение (ничего не блокируется). Подавленное событие всё равно
    помечается доставленным — после unmute накопившееся не уходит."""
    if not chat_id:
        return False
    now = now if now is not None else now_ms()
    if grp != BOT_GRP_SERVICE:
        # мьютинг (all/instrument/zone) и watchlist глушат только торговые
        # события — сервисные сообщения продолжают приходить (ТЗ п.9)
        for m in db.get_mutes(chat_id, now):
            scope, ref = m["scope"], m["scope_ref"]
            if scope == "all":
                return True
            if (scope == "instrument" and instrument_id is not None
                    and ref == str(instrument_id)):
                return True
            if scope == "zone" and zone_id is not None and ref == str(zone_id):
                return True
        if (instrument_id is not None
                and db.watchlist_alerts_disabled(chat_id, instrument_id)):
            return True
    if not db.alert_pref_enabled(chat_id, "global", "", grp, kind):
        return True
    if (instrument_id is not None and not db.alert_pref_enabled(
            chat_id, "instrument", str(instrument_id), grp, kind)):
        return True
    if (zone_id is not None and not db.alert_pref_enabled(
            chat_id, "context", str(zone_id), grp, kind)):
        return True
    return False


def _zone_muted(db: Database, zone_id: int, cycle_id: int, user: str, now: int) -> bool:
    """Mute действует на всю зону/цикл (§9), независимо от вида события.

    set_mute выставляет muted_until на все существующие строки alert_state зоны,
    но строки под ещё не доставленный вид события может не быть — поэтому mute
    проверяем по любой строке (zone_id, cycle_id, user).
    """
    row = db.conn.execute(
        """SELECT MAX(muted_until) AS mu FROM alert_state
           WHERE zone_id=? AND cycle_id=? AND user=?""",
        (zone_id, cycle_id, user),
    ).fetchone()
    mu = row["mu"] if row else None
    return mu is not None and mu > now


def should_notify(
    db: Database,
    event: Event,
    cfg: DetectorConfig,
    user: str = "owner",
) -> bool:
    """True, если событие нужно доставить сейчас.

    Не создаёт никаких записей — чистая проверка (§8: «само истечение срока
    не создаёт событие»).
    """
    now = now_ms()

    # Ручная пауза/отключение глушит зону независимо от вида события (§9)
    if _zone_muted(db, event.zone_id, event.cycle_id, user, now):
        return False

    # События вне списка подавляемых (новые пороги DEPTH_90, BREAKER_CREATED,
    # LEVEL_TAKEN, сервисные и т.д.) всегда проходят
    if event.kind not in SUPPRESSED_KINDS:
        return True

    st = db.get_alert_state(event.zone_id, event.cycle_id, event.kind.value, user)
    if st is None:
        return True  # первый сигнал этого вида по объекту

    # Отсчёт 120 ч — от успешной доставки (§8)
    horizon_ms = cfg.suppress_hours * 3_600_000
    return now - st.last_delivered_at >= horizon_ms


def mark_delivered(
    db: Database,
    event: Event,
    delivered_at: int,
    user: str = "owner",
) -> None:
    """Фиксирует успешную доставку: upsert AlertState (§8).

    Сохраняет muted_until/acknowledged существующей строки — факт новой
    доставки не отменяет ручную паузу или отметку «Изучаю».
    """
    prev = db.get_alert_state(event.zone_id, event.cycle_id, event.kind.value, user)
    db.set_alert_state(
        AlertState(
            zone_id=event.zone_id,
            cycle_id=event.cycle_id,
            event_kind=event.kind.value,
            last_delivered_at=delivered_at,
            user=user,
            muted_until=prev.muted_until if prev else None,
            acknowledged=prev.acknowledged if prev else False,
        )
    )
