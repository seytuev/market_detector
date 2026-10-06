"""Жизненный цикл касаний — APPROACH, TOUCH, DEPTH_50/90, JUMP_THROUGH,
FVG_WEAKENED/FVG_FILLED, учёт заходов (Visit) — единый для всех таймфреймов
(ТЗ «Единый движок HTF/LTF» от 22.09.2026, §2–§4).

Одни функции для live и replay (§11 п.3): наблюдение — это диапазон [lo,hi]
(для тика lo == hi == цена; для исторической свечи — её тень, при этом порядок
цен внутри свечи неизвестен, что фиксируется в evidence события).

Правила порогов (§2, §3, §6, §8, ТЗ §3/§4/§6):
- каждое наблюдение порождает не более одного события глубины — самый высокий
  новый достигнутый порог; первый заход сразу до 90% даёт одно DEPTH_90
  с фактической глубиной, без отдельных TOUCH/DEPTH_50;
- пороги, уже достигнутые в текущем жизненном цикле, повторных событий не
  порождают (сверка с таблицей event через db.get_events);
- проход насквозь (JUMP_THROUGH) не порождает обычный TOUCH (§6, §13.11);
- ТЗ §3: OB любого ТФ НЕ завершается по глубине теста — частичный тест
  (включая 90% и выход тенью за дальнюю границу) пишется как история глубины;
  актуальность (market_validity) теряется только закрытием свечи своего ТФ
  строго за дальней границей (проверка в scanner); рисунок актуального
  протестированного OB продолжается вправо (без display_until по worked_90);
- ТЗ §3: max_test_depth — максимум глубины самостоятельных тестов за всю
  историю действующего OB (консервативная формализация); entry_eligible =
  market_validity active AND max_test_depth строго < entry_reuse_max_depth
  (ровно 90% уже недопустимо; сравнение — точная Decimal-арифметика по
  экстремуму теста, без epsilon);
- ТЗ §4: тест — только самостоятельный возврат ПОСЛЕ выхода из базы; свечи
  базы и первоначальный проход импульса тестами не являются. Тесты до
  подтверждения FVG накапливаются в истории кандидата (silent: визиты и
  глубина пишутся, события не эмитятся) и не обнуляются при подтверждении;
- ТЗ §6: Breaker невалиден при собственном тесте СТРОГО глубже 50% после
  преобразования (ровно 50% допустимо); отдельно сохраняется отмена по
  закрытию за дальней границей (scanner);
- PRB: прежний порог 90% сохраняется — DEPTH_90 → status WORKED;
- FVG: 50% → WEAKENED + FVG_WEAKENED; полное перекрытие → FVG_FILLED
  (зона архивируется); касание связанного OB/PRB проверяется независимо
  по его реальным границам в том же проходе scanner (§3);
- прежняя отдельная ветка «H1: первое касание завершает зону» (§9.8) и
  запрет первой протестированной H1-зоны удалены (ТЗ §2/§6).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..config import DetectorConfig
from ..db import Database
from ..models import (
    Direction,
    Event,
    EventKind,
    TIMEFRAME_MINUTES,
    Visit,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
)
from . import depth as geom

# Пороги глубины по типам зон строятся из DetectorConfig (§2/§3/§6):
# depth_mid (50%) и depth_worked (90%) — согласованные настройки, не константы.
def _block_thresholds(cfg: DetectorConfig) -> list[tuple[float, EventKind]]:
    """Пороги блоков (OB/PRB/Breaker/…) от высшего к низшему."""
    return [
        (cfg.depth_worked, EventKind.DEPTH_90),
        (cfg.depth_mid, EventKind.DEPTH_50),
        (0.0, EventKind.TOUCH),
    ]


def _fvg_thresholds(cfg: DetectorConfig) -> list[tuple[float, EventKind]]:
    """Пороги FVG: полное перекрытие (1.0) — заполнен, середина — ослаблен."""
    return [
        (1.0, EventKind.FVG_FILLED),
        (cfg.depth_mid, EventKind.FVG_WEAKENED),
        (0.0, EventKind.TOUCH),
    ]


def _kind_thresholds(cfg: DetectorConfig) -> dict[EventKind, float]:
    return {
        EventKind.TOUCH: 0.0,
        EventKind.DEPTH_50: cfg.depth_mid,
        EventKind.DEPTH_90: cfg.depth_worked,
        EventKind.FVG_WEAKENED: cfg.depth_mid,
        EventKind.FVG_FILLED: 1.0,
    }


def is_ob_like(zone: Zone) -> bool:
    """Зона с правилами OB (ТЗ §3): автоматический OB или ручная зона
    с типовым правилом ob."""
    return zone.type == ZoneType.OB or (
        zone.type == ZoneType.MANUAL and zone.zone_type == ZoneType.OB.value
    )


def _kind_status(zone: Zone, kind: EventKind) -> Optional[ZoneStatus]:
    """Терминальный статус порога глубины для данного типа зоны.

    ТЗ §3/§6: OB (и ручные ob) по глубине не завершаются — None; прежнее
    правило 90% → WORKED сохраняется только для PRB и ручных зон без
    типового правила; FVG — свои пороги.
    """
    if kind == EventKind.FVG_WEAKENED:
        return ZoneStatus.WEAKENED
    if kind == EventKind.FVG_FILLED:
        return ZoneStatus.ARCHIVED
    if kind == EventKind.DEPTH_90:
        if zone.type == ZoneType.PRB or (
            zone.type == ZoneType.MANUAL and zone.zone_type != ZoneType.OB.value
        ):
            return ZoneStatus.WORKED
        return None
    return None


def is_delayed(cfg: DetectorConfig, occurred_at: int, detected_at: int) -> bool:
    """§11: восстановленное событие — detected существенно позже occurred."""
    return detected_at - occurred_at > cfg.delivery_target_seconds * 1000


def set_zone_end(db: Database, zone: Zone, ts: int, status: Optional[ZoneStatus],
             end_reason: str, force: bool = False) -> None:
    """Терминальный переход: статус + display_until/end_reason — колонки зоны
    (§15.1.3: завершённые объекты не тянутся вправо на экране активных зон).

    ТЗ 06.10.2026 §5/§6 (T06): первое событие пробоя не перезаписывается
    более поздним — display_until/end_reason устанавливаются по самой ранней
    свече события; более поздний вызов меняет только статус. Уточнение
    причины ТОГО ЖЕ события (равный ts — например, close_beyond →
    close_beyond_no_breaker после решения о Breaker) допустимо. force=True —
    явный версионированный перерасчёт (конверсия в Breaker, коррекция
    границ): прежняя дата первого пробоя при этом сохраняется в
    evidence/событии OB_INVALIDATED.
    """
    fields: dict = {}
    if force or zone.display_until is None or ts <= zone.display_until:
        fields["display_until"] = ts
        fields["end_reason"] = end_reason
    if status is not None:
        fields["status"] = status
    if fields:
        db.update_zone(zone.id, **fields)
        zone.display_until = fields.get("display_until", zone.display_until)
        zone.end_reason = fields.get("end_reason", zone.end_reason)
        if status is not None:
            zone.status = status


def _set_evidence(db: Database, zone: Zone, **updates) -> None:
    """Точечное обновление evidence с сохранением source_candles в json
    (db._to_zone восстанавливает их оттуда)."""
    ev = dict(zone.evidence)
    ev.update(updates)
    ev["source_candles"] = zone.source_candles
    db.update_zone(zone.id, evidence=ev)
    zone.evidence = {k: v for k, v in ev.items() if k != "source_candles"}


def _departed_beyond_near(zone: Zone, lo: float, hi: float) -> bool:
    """Наблюдение полностью за ближней границей на стороне импульса
    (ТЗ §4: направленный выход из базы). Бычий OB — весь диапазон выше U."""
    if zone.direction == Direction.BULL:
        return lo > zone.upper
    return hi < zone.lower


def base_end_ms(zone: Zone) -> Optional[int]:
    """Граница закрытия последней свечи базы (exclusive close boundary).

    Нижняя временная граница всех lifecycle-событий кандидата (ТЗ 06.10.2026
    §3.1/§5): выход из базы не может предшествовать завершению самой базы.
    None — свечи базы неизвестны (нет source_candles и formed_at).
    """
    if zone.source_candles:
        last_open = max(zone.source_candles)
    elif zone.formed_at:
        last_open = zone.formed_at
    else:
        return None
    tf = TIMEFRAME_MINUTES.get(zone.timeframe)
    if tf is None:
        return None
    return close_boundary_ms(last_open, zone.timeframe)


def _update_test_stats(db: Database, cfg: DetectorConfig, zone: Zone,
                       extreme: Optional[float]) -> None:
    """ТЗ §3: максимальная глубина самостоятельных тестов за всю историю и
    пригодность для нового входа (точное сравнение по экстремуму, без epsilon).

    Мелкий поздний тест не стирает более глубокий прежний: экстремум только
    углубляется (bull — min Low, bear — max High)."""
    if extreme is None:
        return
    prev = zone.test_extreme
    bull = zone.direction == Direction.BULL
    worse = prev is None or (extreme < prev if bull else extreme > prev)
    new_extreme = extreme if worse else prev
    d = geom.exact_depth(zone, new_extreme)
    d_clamped = max(Decimal(0), min(Decimal(1), d))
    eligible = (
        zone.market_validity == "active"
        and not geom.reaches_depth(zone, new_extreme, cfg.entry_reuse_max_depth)
    )
    # значения не изменились — записи нет: update_zone инвалидирует кэш
    # get_zones, а вызов идёт на каждом касании OB (в т.ч. на каждой свече
    # replay с открытым визитом)
    if (
        zone.has_tests
        and new_extreme == zone.test_extreme
        and float(d_clamped) == zone.max_test_depth
        and eligible == zone.entry_eligible
    ):
        return
    db.update_zone(
        zone.id, test_extreme=new_extreme, max_test_depth=float(d_clamped),
        has_tests=True, entry_eligible=eligible,
    )
    zone.test_extreme = new_extreme
    zone.max_test_depth = float(d_clamped)
    zone.has_tests = True
    zone.entry_eligible = eligible


def _visit_extreme(zone: Zone, visit: Visit, lo: float, hi: float) -> float:
    """Глубочайшая точка захода с учётом текущего наблюдения
    (bull — min Low, bear — max High)."""
    cur = lo if zone.direction == Direction.BULL else hi
    if visit.extreme is None:
        return cur
    return min(visit.extreme, cur) if zone.direction == Direction.BULL \
        else max(visit.extreme, cur)


def track_zone(
    db: Database,
    cfg: DetectorConfig,
    zone: Zone,
    lo: float,
    hi: float,
    ts: int,
    observed: bool,
    prev_price: Optional[float],
    detected_at: int,
    entered_at: Optional[int] = None,
    silent: bool = False,
) -> list[Event]:
    """Обрабатывает одно наблюдение [lo,hi] в момент ts по диапазонной зоне.

    Возвращает список новых событий (уже вставленных в БД). Визиты и статусы
    обновляются в БД. Повторный вызов с теми же данными дублей не создаёт
    (UNIQUE(zone_id, cycle_id, kind, occurred_at) + сверка порогов).

    silent=True — кандидат OB до подтверждения FVG (ТЗ §4): самостоятельные
    тесты пишутся в историю (визиты, max_test_depth), но события не эмитятся
    и уведомления задним числом не отправляются.
    """
    created: list[Event] = []
    delayed = is_delayed(cfg, ts, detected_at)
    ev_base: dict = {"observed": observed}
    if not observed:
        # §11: порядок цен внутри исторической свечи неизвестен
        ev_base["intra_candle_order_unknown"] = True

    ob_like = is_ob_like(zone)

    # ТЗ §4: последовательность — база → направленный выход → самостоятельный
    # возврат (тест). Свечи базы и первоначальный проход импульса через будущий
    # диапазон тестами не являются. Фаза по умолчанию: подтверждённая зона
    # (внешний FVG доказывает выход из базы) — departed; кандидат ждёт
    # наблюдаемого выхода.
    if ob_like:
        be = base_end_ms(zone)
        phase = zone.evidence.get("phase")
        if phase is None:
            phase = "departed" if zone.confirmed_at is not None else "forming"
        import os as _os
        if _os.environ.get("HTF_DEBUG_ZONE") == str(zone.id):
            print(f"DBG ts={ts} ev_phase={zone.evidence.get('phase')} phase={phase} "
                  f"departed_at={zone.evidence.get('departed_at')} be={be} "
                  f"confirmed={zone.confirmed_at} mv={zone.market_validity} "
                  f"lo={lo} hi={hi} intersects={geom.interval_intersects(zone, lo, hi)}",
                  flush=True)
        if phase != "departed":
            # ТЗ 06.10.2026 §3.1/§5: наблюдения до завершения базы не могут
            # быть её выходом — нижняя временная граница сканирования
            if be is not None and ts <= be:
                return created
            if _departed_beyond_near(zone, lo, hi):
                _set_evidence(db, zone, phase="departed", departed_at=ts)
            else:
                if geom.interval_intersects(zone, lo, hi) and not observed:
                    # Свеча могла содержать и выход, и возврат: по одному OHLC
                    # порядок не раскрывается — помечаем неопределённость,
                    # последовательность не выдумываем (ТЗ §4).
                    if not zone.evidence.get("pre_departure_overlap_unknown"):
                        _set_evidence(db, zone, pre_departure_overlap_unknown=True)
                return created
        elif be is not None:
            # Испорченное ранее состояние: выход записан не позднее конца
            # базы — он не может относиться к движению этой базы. Сигналы
            # из такого состояния не порождаем (T02: evidence_incomplete),
            # расхождение фиксируем явно.
            departed_at = zone.evidence.get("departed_at")
            if departed_at is not None and departed_at <= be:
                if zone.evidence.get("integrity") != "inconsistent":
                    _set_evidence(
                        db, zone, integrity="inconsistent",
                        departure_evidence_incomplete=True,
                        inconsistent_reason=(
                            "departed_at не позднее конца базы — выход не "
                            "относится к этой базе (ТЗ 06.10.2026 §3.1)"
                        ),
                    )
                return created

    # точные ключи уже записанных событий — db.insert_event при проигнорированном
    # INSERT OR IGNORE может вернуть устаревший lastrowid (контрактный db.py не
    # меняем), поэтому факт «уже есть» проверяем до вставки
    keys = db.event_keys(zone.id, zone.cycle_id)
    existing = {kind for kind, _ in keys}
    existing_keys = set(keys)
    kind_thr = _kind_thresholds(cfg)
    visit = db.open_visit_for(zone.id, zone.cycle_id)
    import os as _os2
    if _os2.environ.get("HTF_DEBUG_ZONE") == str(zone.id):
        print(f"DBG2 ts={ts} visit={None if visit is None else (visit.id, visit.entered_at, visit.exited_at, visit.max_depth)} "
              f"intersects={geom.interval_intersects(zone, lo, hi)}", flush=True)
    thresholds = (
        _fvg_thresholds(cfg) if zone.type == ZoneType.FVG else _block_thresholds(cfg)
    )

    def emit(kind: EventKind, price: float, depth_val: float, evidence: dict) -> None:
        if silent or (kind, ts) in existing_keys:
            return
        ev = Event(
            id=None, zone_id=zone.id, cycle_id=zone.cycle_id, kind=kind,
            occurred_at=ts, detected_at=detected_at, price=price,
            depth=depth_val, delayed=delayed, evidence=evidence,
        )
        if db.insert_event(ev) is not None:
            created.append(ev)
            existing.add(kind)
            existing_keys.add((kind, ts))

    if geom.interval_intersects(zone, lo, hi):
        dmax = max(0.0, geom.max_depth_in_interval(zone, lo, hi))
        achieved = max(
            (kind_thr[k] for k in existing if k in kind_thr),
            default=-1.0,
        )
        new_kind: Optional[EventKind] = None
        for thr, kind in thresholds:
            if thr <= dmax and thr > achieved:
                new_kind = kind
                break
        if new_kind is not None:
            price = lo if zone.direction == Direction.BULL else hi
            emit(new_kind, price, dmax, {**ev_base, "max_depth": dmax})
            status = _kind_status(zone, new_kind)
            if status is not None:
                if new_kind in (EventKind.DEPTH_90, EventKind.FVG_FILLED):
                    # §15.1.3/§9.5: рисунок заканчивается в момент завершения
                    set_zone_end(db, zone, ts, status,
                                 "worked_90 (§6)" if new_kind == EventKind.DEPTH_90
                                 else "fvg_filled (§3)")
                else:
                    db.update_zone(zone.id, status=status)
        # учёт захода (§8): счётчик визитов и max_depth — разные данные
        if visit is None:
            extreme0 = lo if zone.direction == Direction.BULL else hi
            vid = db.open_visit(Visit(
                id=None, zone_id=zone.id, cycle_id=zone.cycle_id,
                entered_at=entered_at if entered_at is not None else ts,
                max_depth=dmax, observed=observed,
                extreme=extreme0, d_raw=float(geom.exact_depth(zone, extreme0)),
            ))
            visit = db.open_visit_for(zone.id, zone.cycle_id)
            import os as _os3
            if _os3.environ.get("HTF_DEBUG_ZONE") == str(zone.id):
                print(f"DBG3 ts={ts} opened vid={vid} reloaded={None if visit is None else visit.id}",
                      flush=True)
        elif dmax > visit.max_depth:
            extreme = _visit_extreme(zone, visit, lo, hi)
            db.update_visit_depth(visit.id, dmax, extreme=extreme,
                                  d_raw=float(geom.exact_depth(zone, extreme)))
            visit.max_depth = dmax
            visit.extreme = extreme
        # ТЗ §3: статистика самостоятельных тестов OB (в т.ч. до подтверждения)
        if ob_like and visit is not None:
            _update_test_stats(db, cfg, zone, visit.extreme)
        # ТЗ §6: собственный тест Breaker СТРОГО глубже 50% → невалиден
        # (ровно 50% допустимо); раньше правило было только в документации
        if (
            zone.type == ZoneType.BREAKER
            and zone.status == ZoneStatus.ACTIVE
            and visit is not None
            and visit.extreme is not None
            and geom.strictly_deeper(zone, visit.extreme, cfg.depth_mid)
        ):
            set_zone_end(db, zone, ts, ZoneStatus.ARCHIVED,
                         "breaker_test_gt50 (ТЗ §6)")
            emit(EventKind.BREAKER_ARCHIVED, visit.extreme, visit.max_depth, {
                **ev_base, "max_depth": visit.max_depth,
                "note": "собственный тест строго >50% — Breaker невалиден (ТЗ §6)",
            })
            db.close_visit(visit.id, ts, visit.max_depth, exit_kind="invalidated",
                           extreme=visit.extreme)
            return created
        # отработан / полностью перекрыт — заход завершён, касания прекращаются
        # (для OB терминального статуса по глубине нет — заход продолжается)
        if (
            new_kind in (EventKind.DEPTH_90, EventKind.FVG_FILLED)
            and _kind_status(zone, new_kind) is not None
            and visit is not None
        ):
            db.close_visit(visit.id, ts, visit.max_depth,
                           exit_kind="filled" if new_kind == EventKind.FVG_FILLED else "worked",
                           extreme=visit.extreme)
        return created

    # вне зоны
    beyond_far = (
        (zone.direction == Direction.BULL and hi < zone.lower)
        or (zone.direction == Direction.BEAR and lo > zone.upper)
    )
    if beyond_far and visit is not None:
        # Цена побывала внутри (визит открыт) и ушла за дальнюю границу:
        # геометрически все пороги пройдены — фиксируем высший новый
        # (§3: полное перекрытие FVG), затем закрываем заход.
        # ТЗ §3: для OB это НЕ завершение зоны — проверка актуальности идёт
        # по закрытию свечи в scanner; тень за дальней границей — только глубина.
        dmax = max(visit.max_depth, geom.max_depth_in_interval(zone, lo, hi))
        achieved = max(
            (kind_thr[k] for k in existing if k in kind_thr),
            default=-1.0,
        )
        for thr, kind in thresholds:
            if thr <= dmax and thr > achieved:
                price = lo if zone.direction == Direction.BULL else hi
                emit(kind, price, dmax, {
                    **ev_base, "max_depth": dmax,
                    "note": "проход за дальнюю границу внутри захода",
                })
                status = _kind_status(zone, kind)
                if status is not None:
                    if kind in (EventKind.DEPTH_90, EventKind.FVG_FILLED):
                        set_zone_end(db, zone, ts, status,
                                 "worked_90 (§6)" if kind == EventKind.DEPTH_90
                                 else "fvg_filled (§3)")
                    else:
                        db.update_zone(zone.id, status=status)
                break
        extreme = _visit_extreme(zone, visit, lo, hi)
        db.close_visit(visit.id, ts, dmax, exit_kind="beyond", extreme=extreme)
        if ob_like:
            _update_test_stats(db, cfg, zone, extreme)
        return created

    if visit is not None:
        db.close_visit(visit.id, ts, visit.max_depth, exit_kind="return",
                       extreme=visit.extreme)
        visit = None

    if geom.jumped_through(zone, prev_price, lo, hi):
        # §6: проход насквозь — без обычного TOUCH
        if EventKind.JUMP_THROUGH not in existing:
            price = hi if zone.direction == Direction.BULL else lo
            emit(EventKind.JUMP_THROUGH, price, 0.0, {
                **ev_base,
                "prev_price": prev_price,
                "note": "проход зоны насквозь без касания; обычный TOUCH не порождается",
            })
        return created

    # приближение на 2% с правильной стороны (§9)
    dist = geom.approach_distance(zone, lo, hi)
    if (
        dist is not None
        and 0 < dist <= cfg.approach_pct
        and not geom.was_near(zone, prev_price, cfg.approach_pct)
    ):
        price = lo if zone.direction == Direction.BULL else hi
        emit(EventKind.APPROACH, price, 0.0, {**ev_base, "distance_pct": dist})
    return created
