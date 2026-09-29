"""ТЗ «Единый движок HTF/LTF» (22.09.2026) §5: внутренние уровни ликвидности
после теста OB.

Самостоятельный возврат в бычий OB может образовать внутренний SSL на минимуме
теста, в медвежий OB — внутренний BSL на максимуме теста. Подтверждение —
общее строгое правило трёх свечей слева и трёх справа на ТФ уровня (как у
HTF-уровней §7, см. liquidity.find_pivots): High[i] строго выше/ Low[i] строго
ниже всех шести соседей; плато с равными экстремумами не подтверждается.
confirmed_at = закрытие свечи i+3 (open_time(i+3) + длительность ТФ); будущие
свечи заранее не используются.

Если экстремум обновился до подтверждения (новый более глубокий тест), кандидат
пересчитывается (новая цена/pivot_time/source_test_id), старый кандидат
действующим уровнем не становится. Подтверждённые уровни задним числом не
пересчитываются. Экстремум теста вне диапазона OB [lower, upper] уровня не
образует.

Снятие (BSL — рост строго выше уровня, SSL — падение строго ниже, как
level_crossed §7) не отменяет родительский OB и другие уровни; снятые остаются
в истории (status='taken', taken_at). D1- и H1-экстремумы внутри одного OB —
раздельные записи inner_level (разный timeframe), свечи разных ТФ не смешиваются.
"""
from __future__ import annotations

from ..config import DetectorConfig
from ..db import Database
from ..models import Candle, Direction, InnerLevel, Zone


def sync_candidates(
    db: Database, cfg: DetectorConfig, zone: Zone, candles: list[Candle], now: int
) -> list[InnerLevel]:
    """Создаёт/пересчитывает кандидатов внутренних уровней по визитам зоны.

    candles — закрытые свечи ОДНОГО таймфрейма (ТФ уровня = ТФ свечей; свечи
    разных ТФ не смешиваются). Для bull OB kind="ssl" (минимум теста), для
    bear — "bsl" (максимум). pivot_time — open_time свечи, на которой достигнут
    экстремум, в интервале визита [entered_at, exited_at или now].

    На (зона, ТФ, kind) действует один неподтверждённый кандидат: более
    глубокий новый тест пересчитывает его (старый кандидат действующим уровнем
    не становится), более мелкий игнорируется. Пересчитанные экстремумы
    запоминаются в evidence["superseded"], чтобы повторный replay не воскрешал
    старый ключ после переименования строки. Подтверждённые (active/taken)
    уровни задним числом не пересчитываются.

    Идемпотентно: дедуп по UNIQUE(parent_ob_id, timeframe, kind, price,
    pivot_time); повторный вызов с теми же данными записей не дублирует.
    Возвращает список созданных/пересчитанных кандидатов.
    """
    if not candles:
        return []
    tf = candles[0].timeframe
    bull = zone.direction == Direction.BULL
    kind = "ssl" if bull else "bsl"
    visits = sorted(db.get_visits(zone.id), key=lambda v: v.entered_at)
    levels = [
        lv for lv in db.list_inner_levels(parent_ob_id=zone.id)
        if lv.timeframe == tf and lv.kind == kind
    ]
    superseded = {
        (s["price"], s["pivot_time"])
        for lv in levels for s in lv.evidence.get("superseded", [])
    }
    candidates = [lv for lv in levels if lv.status == "candidate"]
    out: list[InnerLevel] = []
    for v in visits:
        if v.extreme is None:
            continue
        price = v.extreme
        if not (zone.lower <= price <= zone.upper):
            continue  # ТЗ §5: экстремум вне диапазона OB — уровень не создаём
        end = v.exited_at if v.exited_at is not None else now
        window = [c for c in candles if v.entered_at <= c.open_time <= end]
        if not window:
            continue
        pivot = (
            min(window, key=lambda c: c.low) if bull
            else max(window, key=lambda c: c.high)
        )
        pivot_time = pivot.open_time
        if db.get_inner_level_by_key(zone.id, tf, kind, price, pivot_time) is not None:
            continue  # дедуп по UNIQUE-ключу
        if (price, pivot_time) in superseded:
            continue  # экстремум пересчитан в более глубокий — не воскрешаем
        cand = next((lv for lv in candidates if lv.source_test_id == v.id), None)
        if cand is None and candidates:
            # новый более глубокий тест до подтверждения пересчитывает прежнего
            # кандидата — старый кандидат действующим уровнем не становится
            deeper = [
                lv for lv in candidates
                if (price < lv.price if bull else price > lv.price)
            ]
            if not deeper:
                continue  # кандидат уже отражает более глубокий тест
            cand = (
                min(deeper, key=lambda lv: lv.price) if bull
                else max(deeper, key=lambda lv: lv.price)
            )
        if cand is not None:
            if cand.price == price and cand.pivot_time == pivot_time:
                continue
            if db.get_inner_level_by_key(zone.id, tf, kind, price, pivot_time) is not None:
                continue  # новый ключ занят другой записью
            ev = dict(cand.evidence)
            sup = list(ev.get("superseded", []))
            old = {"price": cand.price, "pivot_time": cand.pivot_time,
                   "source_test_id": cand.source_test_id}
            if (old["price"], old["pivot_time"]) not in superseded:
                sup.append(old)
            ev["superseded"] = sup
            db.update_inner_level(
                cand.id, price=price, pivot_time=pivot_time,
                source_test_id=v.id, evidence=ev,
            )
            superseded.add((cand.price, cand.pivot_time))
            cand.price, cand.pivot_time = price, pivot_time
            cand.source_test_id, cand.evidence = v.id, ev
            out.append(cand)
            continue
        lv = InnerLevel(
            id=None, parent_ob_id=zone.id, instrument_id=zone.instrument_id,
            timeframe=tf, kind=kind, price=price, pivot_time=pivot_time,
            source_test_id=v.id, status="candidate",
            evidence={
                "rule": "ТЗ «Единый движок» §5: экстремум самостоятельного теста OB; "
                        "подтверждение — строгое 3+3 на ТФ уровня",
            },
            created_at=now,
        )
        lid = db.insert_inner_level(lv)
        if lid is not None:
            lv.id = lid
            candidates.append(lv)
            out.append(lv)
    return out


def confirm_levels(
    db: Database,
    cfg: DetectorConfig,
    zone: Zone,
    candles: list[Candle],
    tf_ms: int,
    boundary: int,
    now: int,
) -> list[InnerLevel]:
    """Подтверждение кандидатов строгим правилом 3+3 по свечам того же ТФ.

    Ждём закрытия третьей правой свечи; confirmed_at = open_time(i+3) + tf_ms
    и не может быть позже boundary текущей обработанной свечи (будущие свечи
    не используются). Если экстремум на pivot-свече не совпадает с кандидатом
    (пересчитался) — кандидат пропускается. Плато (равные экстремумы) строгим
    правилом не подтверждается.
    """
    if not candles:
        return []
    tf = candles[0].timeframe
    left, right = cfg.inner_level_pivot_left, cfg.inner_level_pivot_right
    index = {c.open_time: i for i, c in enumerate(candles)}
    out: list[InnerLevel] = []
    for lv in db.list_inner_levels(parent_ob_id=zone.id, statuses=("candidate",)):
        if lv.timeframe != tf:
            continue  # свой ТФ подтверждают свои свечи
        i = index.get(lv.pivot_time)
        if i is None:
            continue
        c = candles[i]
        pivot_val = c.low if lv.kind == "ssl" else c.high
        if pivot_val != lv.price:
            continue  # экстремум на pivot-свече пересчитался
        if i - left < 0 or i + right >= len(candles):
            continue  # ждём три левые/правые закрытые свечи
        confirmed_at = candles[i + right].open_time + tf_ms
        if confirmed_at > boundary:
            continue  # не подтверждаем раньше времени
        others = [
            w for w in candles[i - left: i + right + 1] if w.open_time != c.open_time
        ]
        if lv.kind == "ssl":
            ok = all(c.low < w.low for w in others)
        else:
            ok = all(c.high > w.high for w in others)
        if not ok:
            continue  # строгое правило: равные экстремумы не подтверждаются
        db.update_inner_level(lv.id, status="active", confirmed_at=confirmed_at)
        lv.status, lv.confirmed_at = "active", confirmed_at
        out.append(lv)
    return out


def level_taken(lv: InnerLevel, lo: float, hi: float) -> bool:
    """Снятие уровня наблюдаемым диапазоном [lo,hi] (как level_crossed §7):
    BSL — рост строго выше уровня, SSL — падение строго ниже."""
    if lv.kind == "bsl":
        return hi > lv.price
    return lo < lv.price


def check_taken_levels(
    db: Database, levels: list[InnerLevel], lo: float, hi: float, ts: int
) -> list[InnerLevel]:
    """Переводит пересечённые active-уровни в taken (taken_at=ts).

    Уровень недоступен снятию раньше confirmed_at (§13.4 по аналогии);
    снятые остаются в истории и не реактивируются, taken_at назад не двигается.
    Родительский OB и другие уровни не меняются.
    """
    taken: list[InnerLevel] = []
    for lv in levels:
        if lv.status != "active":
            continue
        if lv.confirmed_at is None or lv.confirmed_at > ts:
            continue  # уровня для алгоритма ещё нет
        if not level_taken(lv, lo, hi):
            continue
        db.update_inner_level(lv.id, status="taken", taken_at=ts)
        lv.status, lv.taken_at = "taken", ts
        taken.append(lv)
    return taken


def check_taken(
    db: Database, zone: Zone, lo: float, hi: float, ts: int
) -> list[InnerLevel]:
    """Снятие active-уровней одной зоны по диапазону наблюдения [lo,hi]."""
    levels = db.list_inner_levels(parent_ob_id=zone.id, statuses=("active",))
    return check_taken_levels(db, levels, lo, hi, ts)


def check_taken_for_instrument(
    db: Database, instrument_id: int, lo: float, hi: float, ts: int
) -> list[InnerLevel]:
    """Снятие active-уровней всего инструмента (одна выборка на наблюдение)."""
    levels = db.list_inner_levels(instrument_id=instrument_id, statuses=("active",))
    return check_taken_levels(db, levels, lo, hi, ts)
