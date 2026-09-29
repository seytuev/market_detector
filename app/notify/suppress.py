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

from ..config import DetectorConfig
from ..db import Database
from ..models import SUPPRESSED_KINDS, AlertState, Event, now_ms


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
