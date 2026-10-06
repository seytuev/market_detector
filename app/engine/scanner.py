"""Оркестратор детектора (§11 п.3): закрытые свечи создают/подтверждают зоны,
текущие цены отслеживают касания. Одни правила для live и replay.

Все вставки идемпотентны (INSERT OR IGNORE по дедуп-индексам зон и событий),
поэтому повторный вызов с той же свечой и повторный replay дублей не создают
(§13.12). Структурные зоны доступны tick-логике не раньше confirmed_at —
в replay OB «появляется» только когда replay-время дошло до подтверждения
(§4, приёмка §13.19). Сохранённые активные зоны при replay не удаляются,
даже если formed_at вышло за окно lookback (§1).
"""
from __future__ import annotations

from typing import Optional

from ..config import DetectorConfig
from ..db import Database
from ..models import (
    Candle,
    Direction,
    Event,
    EventKind,
    TIMEFRAME_MINUTES,
    Zone,
    ZoneRelation,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
    now_ms,
)
from . import breaker as br
from . import inner_liquidity as inner
from . import liquidity as liq
from . import orderblock as ob_mod
from . import prb as prb_mod
from .fvg import scan_fvgs
from .lifecycle import base_end_ms, is_delayed, is_ob_like, set_zone_end, track_zone

# Диапазонные зоны, отслеживаемые tick-логикой
RANGE_TYPES = {ZoneType.FVG, ZoneType.OB, ZoneType.PRB, ZoneType.BREAKER, ZoneType.MANUAL}
# Касания отслеживаются по ACTIVE/WEAKENED, а также по подтверждённым
# (confirmed_at задан) авто-кандидатам — режим «уведомлять только о
# проверенных» (§10) включается отдельно на уровне доставки.
TRACKED_STATUSES = [ZoneStatus.CANDIDATE, ZoneStatus.ACTIVE, ZoneStatus.WEAKENED]


def review_replay_start_ms(zone: Zone, margin_candles: int = 10) -> Optional[int]:
    """Нижняя граница окна replay при ревью зоны (R02/§15.1.2).

    Состояние зоны пересчитывается по истории от её появления (для OB
    formed_at — open_time ПЕРВОЙ свечи базы, поэтому вся база попадает в
    окно) с запасом в несколько свечей её ТФ (тройка FVG, 3+3 пивотов).
    Более ранняя история не нужна: визиты/уровни/события до окна уже
    записаны live-трекингом, а replay аддитивен и идемпотентен (§13.12) —
    он не теряет и не портит накопленное состояние. None — полный прогон
    (нет временных меток у зоны).
    """
    starts = [
        t for t in (zone.display_from, zone.formed_at, zone.confirmed_at,
                    zone.anchor_time)
        if t is not None
    ]
    tf_min = TIMEFRAME_MINUTES.get(zone.timeframe)
    if not starts or tf_min is None:
        return None
    return max(0, min(starts) - margin_candles * tf_min * 60_000)


class Scanner:
    def __init__(self, db: Database, cfg: DetectorConfig):
        self.db = db
        self.cfg = cfg
        # последняя наблюдавшаяся цена инструмента — для детекции скачков (§6)
        self._last_price: dict[int, float] = {}

    # ------------------------------------------------------------------
    # закрытая свеча: структурные обновления
    # ------------------------------------------------------------------

    def on_closed_candle(
        self, candle: Candle, candles: Optional[list[Candle]] = None
    ) -> list[Event]:
        """Вставляет свечу и пересчитывает структуру её инструмента/ТФ.

        Идемпотентно: повторный вызов с той же свечой дублей не создаёт.
        candles — необязательный in-memory список закрытых свечей ТФ по
        текущую включительно (replay держит его нарастающим и не перечитывает
        всю историю из БД на каждую свечу); None — читаем из БД (live).
        """
        self.db.insert_candles([candle])
        created: list[Event] = []
        iid, tf = candle.instrument_id, candle.timeframe
        boundary = close_boundary_ms(candle.open_time, tf)
        now = now_ms()
        if candles is None:
            candles = self.db.get_candles(iid, tf)

        created += self._scan_fvg_and_blocks(iid, tf, candles, now)
        created += self._check_block_transitions(iid, tf, candle, candles, boundary, now)
        if tf in ("D1", "W1"):
            self._scan_pivots(iid, tf, candles, now)
        created += self._check_level_crossings(iid, candle.low, candle.high, boundary, now)
        self._sync_inner_levels(iid, tf, candle, candles, boundary, now)
        if tf == "D1":
            created += self._check_d1_close_inside(iid, candle, boundary, now, candles)
        return created

    # ----- FVG → OB/PRB -----

    def _scan_fvg_and_blocks(
        self, iid: int, tf: str, candles: list[Candle], now: int
    ) -> list[Event]:
        created: list[Event] = []
        fvgs = scan_fvgs(candles, tf)

        fvg_zone_ids: dict[int, int] = {}
        for f in fvgs:
            z = Zone(
                id=None, instrument_id=iid, type=ZoneType.FVG, direction=f.direction,
                timeframe=tf, lower=f.lower, upper=f.upper, formed_at=f.formed_at,
                confirmed_at=f.confirmed_at, status=ZoneStatus.ACTIVE,
                source="auto", rule_version=self.cfg.rule_version,
                source_candles=list(f.candle_open_times),
                evidence={
                    "rule": "§3: трёхсвечный FVG, строгие неравенства, подтверждение закрытием 3-й свечи",
                },
                # §15.1.7/§9.7: рисунок начинается от средней свечи тройки;
                # торговое подтверждение — по-прежнему закрытие третьей
                display_from=f.candle_open_times[1],
                created_at=now,
            )
            zid = self._insert_zone(z)
            if zid is not None:
                fvg_zone_ids[f.formed_at] = zid

        for f in fvgs:
            base = ob_mod.find_base(candles, f, self.cfg)
            if base is None:
                continue
            if base.gap_candles > self.cfg.uncalibrated_ob_delay_max_candles:
                continue  # отложенный FVG за пределами рабочего окна (§14.2)
            ztype = ZoneType.PRB if tf == "H1" else ZoneType.OB
            external = ob_mod.is_external(base, f)
            parent: Optional[Zone] = None
            if ztype == ZoneType.PRB:
                obs = self.db.get_zones(iid, types=[ZoneType.OB])
                parent = prb_mod.find_parent_ob(base, tf, obs)
            evidence = dict(base.evidence)
            evidence["external_fvg"] = external
            if ztype == ZoneType.PRB:
                evidence["source_timeframe"] = tf  # §5
                evidence["parent_ob_id"] = parent.id if parent else None
                if parent is None:
                    evidence["parent_note"] = "родительский HTF-OB не найден (рабочая схема §5/§14.2)"
            z = Zone(
                id=None, instrument_id=iid, type=ztype, direction=base.direction,
                timeframe=tf, lower=base.lower, upper=base.upper,
                formed_at=base.formed_at,
                confirmed_at=f.confirmed_at if external else None,
                # §4/§10: автоматическая разметка — кандидат для ручной проверки
                status=ZoneStatus.CANDIDATE,
                source="auto", rule_version=self.cfg.rule_version,
                source_candles=base.source_candles, evidence=evidence, created_at=now,
            )
            zid = self._insert_zone(z)
            if zid is None:
                continue
            zone = self.db.get_zone(zid)
            if parent is not None:
                self.db.set_relation(ZoneRelation(zone_id=zid, parent_ob_id=parent.id))
            if not external:
                continue
            if zone.market_validity == "invalid":
                # ТЗ 06.10.2026 §6: база уже уничтожена до подтверждения —
                # поздний FVG не воскрешает старую конструкцию
                continue
            fzid = fvg_zone_ids.get(f.formed_at)
            if fzid is not None:
                self.db.set_relation(ZoneRelation(zone_id=zid, confirming_fvg_id=fzid))
            if zone is None:
                continue
            already = zone.confirmed_at is not None
            if already and zone.confirmed_at != f.confirmed_at:
                # подтверждён более ранним FVG — первое основание не
                # подменяется поздним (ТЗ 06.10.2026 §7)
                continue
            need_ev = (
                zone.evidence.get("confirming_fvg_formed_at") != f.formed_at
                or "actual_confirming_fvg" not in zone.evidence
            )
            if not already:
                self.db.update_zone(zid, confirmed_at=f.confirmed_at)
            # ТЗ 06.10.2026 §7 (T08): evidence фактического подтверждения и
            # relation ссылаются на один и тот же FVG с одной тройкой —
            # и при подтверждении в момент создания зоны, и при позднем
            if need_ev:
                ev_dict = dict(zone.evidence)
                ev_dict.update({
                    "external_fvg": True,
                    "confirming_fvg_range": [f.lower, f.upper],
                    "confirming_fvg_formed_at": f.formed_at,
                    "actual_confirming_fvg": {
                        "formed_at": f.formed_at,
                        "open_times": list(f.candle_open_times),
                        "range": [f.lower, f.upper],
                    },
                })
                # ТЗ §4: внешний FVG доказывает направленный выход из базы —
                # самостоятельные возвраты после подтверждения являются тестами.
                # Фаза и ранняя silent-история кандидата не перезаписываются.
                ev_dict.setdefault("phase", "departed")
                ev_dict.setdefault("departed_at", f.confirmed_at)
                self._update_evidence(zone, ev_dict)
            ev = Event(
                id=None, zone_id=zid, cycle_id=zone.cycle_id,
                kind=EventKind.OB_CONFIRMED, occurred_at=f.confirmed_at,
                detected_at=now, price=f.lower,
                delayed=is_delayed(self.cfg, f.confirmed_at, now),
                evidence={"confirming_fvg_zone_id": fzid,
                          "confirming_fvg_range": [f.lower, f.upper]},
            )
            ev = self._emit(ev)
            if ev is not None:
                created.append(ev)
        return created

    # ----- Breaker / архивация блоков -----

    def _check_block_transitions(
        self, iid: int, tf: str, candle: Candle, candles: list[Candle],
        boundary: int, now: int
    ) -> list[Event]:
        created: list[Event] = []

        # OB → Breaker (§6 + §15.6/§9.5–6): нужны ОБА условия — закрытие свечи
        # ТФ зоны за дальней границей И новый FVG пробойного движения.
        # Предыдущий отдельный тест >50% навсегда исключает преобразование.
        # ТЗ 06.10.2026 §6: пробой ищется ретросканированием от момента
        # наблюдаемости зоны (confirmed_at; для кандидата — конец базы), а не
        # только на текущей свече — display_until получает границу закрытия
        # ПЕРВОЙ пробойной свечи (T03–T05), позднее не перезаписывается (T06).
        tf_ms = TIMEFRAME_MINUTES[tf] * 60_000
        for ob in self.db.get_zones(
            iid,
            statuses=[ZoneStatus.CANDIDATE, ZoneStatus.ACTIVE, ZoneStatus.WORKED],
            types=[ZoneType.OB],
            timeframes={tf},
        ):
            pending_at = ob.breakout_close_at
            if ob.market_validity == "invalid" and pending_at is None:
                continue  # первый пробой уже зафиксирован, решение принято
            scan_from = ob.confirmed_at
            if scan_from is None:
                # кандидат до подтверждения: история раннего кандидата тоже
                # проверяется — база, уничтоженная до FVG, не воскресает
                # поздним подтверждением (ТЗ 06.10.2026 §6)
                scan_from = base_end_ms(ob)
            if scan_from is None or scan_from > boundary:
                continue
            if pending_at is not None:
                first_b = None  # пробой уже зафиксирован ранее
            else:
                first_b = br.first_close_beyond(candles, ob, scan_from, boundary)
            if first_b is not None:
                breakout_boundary = close_boundary_ms(first_b.open_time, tf)
                # ТЗ §3: закрытие свечи ТФ зоны СТРОГО за дальней границей —
                # потеря рыночной актуальности OB независимо от дальнейшего
                # образования Breaker; равенство Close границе — не пробой
                # (строгие неравенства в breaker.far_boundary_broken)
                self.db.update_zone(ob.id, market_validity="invalid",
                                    entry_eligible=False)
                set_zone_end(self.db, ob, breakout_boundary, None,
                             "close_beyond (ТЗ §3)")
                # журнал доказательств (ТЗ 06.10.2026 §14): свеча первого
                # пробоя, OHLC, сравниваемая граница, версии правил
                if ob.evidence.get("invalidated_at") is None:
                    ev_d = dict(ob.evidence)
                    ev_d["invalidated_at"] = breakout_boundary
                    ev_d["first_invalidating_candle_open_time"] = first_b.open_time
                    self._update_evidence(ob, ev_d)
                    ob.evidence = ev_d
                ev = Event(
                    id=None, zone_id=ob.id, cycle_id=ob.cycle_id,
                    kind=EventKind.OB_INVALIDATED, occurred_at=breakout_boundary,
                    detected_at=now, price=first_b.close,
                    delayed=is_delayed(self.cfg, breakout_boundary, now),
                    evidence={
                        "candle_open_time": first_b.open_time,
                        "open": first_b.open, "high": first_b.high,
                        "low": first_b.low, "close": first_b.close,
                        "boundary_compared": (
                            ob.lower if ob.direction == Direction.BULL else ob.upper
                        ),
                        "rule_version": ob.rule_version,
                    },
                )
                ev = self._emit(ev)
                if ev is not None:
                    created.append(ev)
                if ob.confirmed_at is None:
                    # база уничтожена до подтверждения — поздний FVG не
                    # воскрешает конструкцию, Breaker не строится (ТЗ §6)
                    continue
                if br.breaker_forbidden(
                    ob, self.db.get_visits(ob.id, ob.cycle_id), candles, self.cfg,
                    before_ms=first_b.open_time,
                ):
                    if not ob.breaker_forbidden:
                        self.db.update_zone(ob.id, breaker_forbidden=True)
                        ev_d = dict(ob.evidence)
                        ev_d["breaker_forbidden_reason"] = (
                            "предыдущий отдельный тест >50% исключает Breaker (§15.6)"
                        )
                        self._update_evidence(ob, ev_d)
                    # ТЗ 06.10.2026 §10 (T20): пробитый OB неактуален и при
                    # запрещённой конверсии — не остаётся active/candidate
                    set_zone_end(self.db, ob, breakout_boundary,
                                 ZoneStatus.ARCHIVED, "close_beyond_no_breaker (§15.6)")
                    continue
                fvg = br.find_breakout_fvg(candles, ob, first_b.open_time, self.cfg)
                if fvg is not None:
                    activate_at = max(breakout_boundary, fvg.confirmed_at)
                    created += self._create_breaker(ob, activate_at, now,
                                                    first_b.close, fvg)
                else:
                    # §9.6: закрытие за границей есть, нового FVG пробоя пока
                    # нет — активного Breaker ещё нет, ждём его подтверждения
                    self.db.update_zone(ob.id, breakout_close_at=breakout_boundary)
                continue
            if pending_at is None:
                continue
            # ожидание нового FVG пробойного движения (может подтвердиться
            # позже пробойного закрытия — приёмка §15.6)
            delay_ms = self.cfg.uncalibrated_breakout_fvg_delay_candles * tf_ms
            if boundary > pending_at + delay_ms:
                if not ob.breakout_expired:
                    # окно FVG пробоя истекло: OB уже неактуален (close_beyond),
                    # конверсии нет — уходит в архив
                    self.db.update_zone(ob.id, breakout_expired=True,
                                        status=ZoneStatus.ARCHIVED)
                continue
            fvg = br.find_breakout_fvg(candles, ob, pending_at - tf_ms, self.cfg)
            if fvg is not None:
                activate_at = max(pending_at, fvg.confirmed_at)
                created += self._create_breaker(ob, activate_at, now, candle.close, fvg)

        # Пробитый Breaker → ARCHIVED, обратного превращения нет (§13.16).
        # PRB при аналогичном закрытии архивируется без Breaker (§5, §13.10).
        blocks = self.db.get_zones(iid, statuses=[ZoneStatus.ACTIVE], types=[ZoneType.BREAKER],
                                   timeframes={tf})
        blocks += self.db.get_zones(iid, statuses=TRACKED_STATUSES, types=[ZoneType.PRB],
                                    timeframes={tf})
        for z in blocks:
            if not self._visible(z, tf, boundary):
                continue
            if not br.far_boundary_broken(z, candle):
                continue
            # §15.1.3: рисунок заканчивается моментом завершения состояния
            set_zone_end(self.db, z, boundary, ZoneStatus.ARCHIVED,
                         "breaker_broken (§6)" if z.type == ZoneType.BREAKER
                         else "prb_broken (§5)")
            kind = (EventKind.BREAKER_ARCHIVED if z.type == ZoneType.BREAKER
                    else EventKind.PRB_ARCHIVED)
            ev = Event(
                id=None, zone_id=z.id, cycle_id=z.cycle_id, kind=kind,
                occurred_at=boundary, detected_at=now, price=candle.close,
                delayed=is_delayed(self.cfg, boundary, now),
                evidence={"close": candle.close, "lower": z.lower, "upper": z.upper},
            )
            ev = self._emit(ev)
            if ev is not None:
                created.append(ev)

        # ТЗ §7: ручные зоны с типовым правилом ob теряют актуальность по тому
        # же закрытию строго за дальней границей (без конверсии в Breaker);
        # ручное подтверждение границ не обходит пробой по истории
        for z in self.db.get_zones(iid, statuses=TRACKED_STATUSES,
                                   types=[ZoneType.MANUAL], timeframes={tf}):
            if z.zone_type != ZoneType.OB.value or z.market_validity != "active":
                continue
            start = z.confirmed_at or z.anchor_time or z.display_from or z.formed_at
            if start > boundary:
                continue
            if br.far_boundary_broken(z, candle):
                self.db.update_zone(z.id, market_validity="invalid",
                                    entry_eligible=False)
                set_zone_end(self.db, z, boundary, ZoneStatus.ARCHIVED,
                             "close_beyond (ТЗ §3)")
        return created

    def _create_breaker(
        self, ob: Zone, activate_at: int, now: int, price: float, fvg
    ) -> list[Event]:
        """Создание Breaker при выполнении обоих условий §15.6.

        Сегмент исходного OB заканчивается конверсией (§15.1.4), Breaker —
        новая зона с теми же границами и своим циклом.
        """
        bz = br.make_breaker(ob, activate_at, now, fvg)
        bid = self._insert_zone(bz)
        if bid is None:
            return []
        self.db.set_relation(ZoneRelation(zone_id=bid, predecessor_ob_id=ob.id))
        # сегмент OB заканчивается конверсией (§15.1.4); дата первого пробоя
        # сохранена в evidence.invalidated_at и событии OB_INVALIDATED
        set_zone_end(self.db, ob, activate_at, ZoneStatus.CONVERTED,
                     "converted_to_breaker (§6/§15.6)", force=True)
        ev = Event(
            id=None, zone_id=bid, cycle_id=bz.cycle_id,
            kind=EventKind.BREAKER_CREATED, occurred_at=activate_at,
            detected_at=now, price=price,
            delayed=is_delayed(self.cfg, activate_at, now),
            evidence={
                "predecessor_ob_id": ob.id,
                "breakout_fvg_formed_at": fvg.formed_at if fvg else None,
            },
        )
        ev = self._emit(ev)
        return [ev] if ev is not None else []

    # ----- SSL/BSL -----

    def _scan_pivots(self, iid: int, tf: str, candles: list[Candle], now: int) -> None:
        pivots = liq.find_pivots(candles, tf, self.cfg)
        for p in pivots:
            z = Zone(
                id=None, instrument_id=iid, type=p.kind, direction=(
                    # BSL — снятие ожидается ростом; реакция после снятия — вниз,
                    # но направление зоны здесь не используется (уровень, одна цена)
                    Direction.BEAR if p.kind == ZoneType.BSL else Direction.BULL
                ),
                timeframe=tf, lower=p.price, upper=p.price, formed_at=p.formed_at,
                confirmed_at=p.confirmed_at, status=ZoneStatus.ACTIVE,
                source="auto", rule_version=self.cfg.rule_version,
                source_candles=list(p.candle_open_times),
                evidence={"rule": "§7: pivot 3+3 по теням, строгие сравнения; "
                                  "плато с равными пиками — открытое решение §14.3"},
                created_at=now,
            )
            self._insert_zone(z)
        self._recluster_levels(iid)

    def _recluster_levels(self, iid: int) -> None:
        """Перестраивает группы экстремумов (§7) и обновляет качественный
        контекст «часть группы снята» — без баллов (§7). Снятые участники
        в активные не возвращаются (§13.17): статусы здесь не меняются.
        """
        for ztype, take_max in ((ZoneType.BSL, True), (ZoneType.SSL, False)):
            zones = [z for z in self.db.get_zones(iid, types=[ztype])
                     if z.status != ZoneStatus.REJECTED]
            if not zones:
                continue
            groups = liq.cluster_prices(
                [z.lower for z in zones], self.cfg.cluster_tolerance_pct
            )
            for group in groups:
                members = [zones[i] for i in group]
                gid = min(m.formed_at for m in members)
                prices = [m.lower for m in members]
                level = max(prices) if take_max else min(prices)
                any_taken = any(m.status == ZoneStatus.TAKEN for m in members)
                for m in members:
                    ev = dict(m.evidence)
                    ev.update({
                        "group_id": gid,
                        "group_level": level,
                        "group_members": [x.id for x in members],
                        "is_extreme": m.lower == level,
                        # качественный контекст авторской модели, без баллов (§7)
                        "part_taken": any_taken and m.status != ZoneStatus.TAKEN,
                    })
                    self._update_evidence(m, ev)

    def _check_level_crossings(
        self, iid: int, lo: float, hi: float, ts: int, detected_at: int
    ) -> list[Event]:
        """Пересечение уровней SSL/BSL: исторически — по теням закрытых свечей,
        в live — по тику (lo == hi). Снятый уровень не реактивируется (§7)."""
        zones = self.db.get_zones(
            iid, statuses=[ZoneStatus.ACTIVE], types=[ZoneType.SSL, ZoneType.BSL]
        )
        return self._cross_level_zones(zones, iid, lo, hi, ts, detected_at)

    def _cross_level_zones(
        self, zones: list[Zone], iid: int, lo: float, hi: float, ts: int, detected_at: int
    ) -> list[Event]:
        created: list[Event] = []
        for z in zones:
            if z.confirmed_at is None or z.confirmed_at > ts:
                continue  # уровня для алгоритма ещё нет (§13.4)
            if z.status != ZoneStatus.ACTIVE:
                continue
            if not liq.level_crossed(z, lo, hi):
                continue
            # §15.1.3: снятый уровень заканчивается в swept_at, не воскресает (§15.1.5)
            set_zone_end(self.db, z, ts, ZoneStatus.TAKEN, "swept (§7)")
            ev = Event(
                id=None, zone_id=z.id, cycle_id=z.cycle_id,
                kind=EventKind.LEVEL_TAKEN, occurred_at=ts, detected_at=detected_at,
                price=z.lower, delayed=is_delayed(self.cfg, ts, detected_at),
                evidence={"cross_lo": lo, "cross_hi": hi, "level": z.lower},
            )
            ev = self._emit(ev)
            if ev is not None:
                created.append(ev)
        if created:
            self._recluster_levels(iid)  # обновить флаг «часть группы снята»
        return created

    # ----- внутренние уровни ликвидности OB (ТЗ §5) -----

    def _sync_inner_levels(
        self, iid: int, tf: str, candle: Candle, candles: list[Candle],
        boundary: int, now: int
    ) -> None:
        """Внутренние BSL/SSL после теста OB-like зон по свечам одного ТФ.

        Таймфреймы не смешиваются: уровень наследует ТФ обрабатываемой свечи,
        поэтому внутри D1 OB D1- и H1-экстремумы — раздельные записи (H1-уровни
        появляются при обработке H1-свечей). Зоны своего и более крупного ТФ
        (уровень младшего ТФ внутри старшего OB); архивированные/отклонённые
        пропускаются. Снятие проверяется одной выборкой по инструменту —
        уровень может быть подтверждён на другом ТФ.
        """
        tf_ms = TIMEFRAME_MINUTES[tf] * 60_000
        zones = [
            z for z in self.db.get_zones(iid)
            if is_ob_like(z)
            and z.status not in (ZoneStatus.ARCHIVED, ZoneStatus.REJECTED)
            and TIMEFRAME_MINUTES[z.timeframe] >= TIMEFRAME_MINUTES[tf]
        ]
        # индексы по свечам строятся один раз на свечу и разделяются всеми
        # зонами (bisect-срезы окна визита / поиск pivot-свечи); чтения
        # визитов и уровней внутри — через версионные кэши Database
        times = [c.open_time for c in candles]
        index = {t: i for i, t in enumerate(times)}
        for z in zones:
            inner.sync_candidates(self.db, self.cfg, z, candles, now, times=times)
            inner.confirm_levels(self.db, self.cfg, z, candles, tf_ms, boundary, now,
                                 index=index)
        inner.check_taken_for_instrument(self.db, iid, candle.low, candle.high, boundary)

    # ----- D1 close inside (§9) -----

    def _check_d1_close_inside(
        self, iid: int, candle: Candle, boundary: int, now: int,
        candles: Optional[list[Candle]] = None,
    ) -> list[Event]:
        created: list[Event] = []
        # §8: ежедневные напоминания отменены — «закрепление внутри» эмитим
        # только при переходе: предыдущее D1-закрытие было вне зоны
        if candles is not None:
            # replay: in-memory список по текущую свечу включительно
            prev_close = candles[-2].close if len(candles) >= 2 else None
        else:
            prev = self.db.get_candles(iid, "D1", end_ms=candle.open_time - 1)
            prev_close = prev[-1].close if prev else None
        for z in self.db.get_zones(iid, statuses=TRACKED_STATUSES):
            if z.type not in RANGE_TYPES or z.is_level:
                continue
            if z.confirmed_at is None or z.confirmed_at > boundary:
                continue
            if not z.lower <= candle.close <= z.upper:
                continue
            if prev_close is not None and z.lower <= prev_close <= z.upper:
                continue  # цена закрылась в зоне и вчера — не новое событие
            ev = Event(
                id=None, zone_id=z.id, cycle_id=z.cycle_id,
                kind=EventKind.D1_CLOSE_INSIDE, occurred_at=boundary,
                detected_at=now, price=candle.close,
                delayed=is_delayed(self.cfg, boundary, now),
                evidence={"timeframe_of_close": "D1"},
            )
            ev = self._emit(ev)
            if ev is not None:
                created.append(ev)
        return created

    # ------------------------------------------------------------------
    # текущая цена: касания / глубина / скачки
    # ------------------------------------------------------------------

    def on_price(
        self, instrument_id: int, price: float, ts_ms: int, observed: bool = True,
        timeframes: Optional[set] = None,
    ) -> list[Event]:
        """Тик текущей цены. Отслеживает ACTIVE/WEAKENED/подтверждённых
        кандидатов; WORKED пропускаются (у них только проверка Breaker
        в on_closed_candle), TAKEN/ARCHIVED — тоже.
        timeframes — необязательное ограничение ТФ отслеживаемых зон (настройка
        scan_timeframes применяется воркером)."""
        prev = self._last_price.get(instrument_id)
        created = self._track_all(
            instrument_id, price, price, ts_ms, observed, prev, now_ms(),
            timeframes=timeframes,
        )
        self._last_price[instrument_id] = price
        return created

    def _track_all(
        self,
        iid: int,
        lo: float,
        hi: float,
        ts: int,
        observed: bool,
        prev: Optional[float],
        detected_at: int,
        entered_at: Optional[int] = None,
        timeframes: Optional[set] = None,
    ) -> list[Event]:
        created: list[Event] = []
        zones = self.db.get_zones(iid, statuses=TRACKED_STATUSES, timeframes=timeframes)
        # Структурная зона доступна tick-логике строго ПОСЛЕ confirmed_at (§4):
        # свеча, чьё закрытие подтвердило зону, обрабатывается до создания зоны,
        # поэтому при replay видимость не зависит от того, когда строка создана —
        # это делает повторный replay идемпотентным (§13.12).
        # Для ручных зон (ТЗ §7) началом видимости служит выбранный anchor
        # (anchor_time/display_from/formed_at) — confirmed_at алгоритмом не
        # проставляется. ТЗ §3: неактуальные (market_validity=invalid) и
        # ожидающие FVG пробоя зоны касаниями не отслеживаются.
        def _track_start(z: Zone) -> Optional[int]:
            if z.source == "manual":
                return z.anchor_time or z.display_from or z.formed_at
            return z.confirmed_at

        visible = [z for z in zones
                   if (s := _track_start(z)) is not None and s < ts
                   # §9.6: OB с пробойным закрытием (ждёт новый FVG пробоя)
                   # и OB, навсегда лишённый Breaker, касаниями не отслеживаются
                   and z.breakout_close_at is None
                   and z.market_validity == "active"
                   and not z.breaker_forbidden]
        # ТЗ §4: кандидаты OB до подтверждения FVG отслеживаются молча —
        # самостоятельные тесты после выхода из базы пишутся в историю
        # (визиты, max_test_depth), события и уведомления не порождаются.
        # ТЗ 06.10.2026 §3.1: нижняя граница — закрытие последней свечи базы;
        # более ранние наблюдения не могут быть выходом именно этой базы
        # (иначе replay писал departed_at раньше formed_at — зоны №550/549/…)
        silent = [z for z in zones
                  if z.type == ZoneType.OB and z.source != "manual"
                  and (z.confirmed_at is None or z.confirmed_at >= ts)
                  and z.breakout_close_at is None
                  and z.market_validity == "active"
                  and (be := base_end_ms(z)) is not None and be < ts]
        levels = [z for z in visible if z.is_level]
        if levels:
            created += self._cross_level_zones(levels, iid, lo, hi, ts, detected_at)
        # ТЗ §5: снятие подтверждённых внутренних уровней ликвидности OB —
        # одна выборка active-уровней инструмента на наблюдение (без sync —
        # кандидаты пересчитываются только на закрытых свечах)
        inner.check_taken_for_instrument(self.db, iid, lo, hi, ts)
        for z in visible:
            if not z.is_level and z.type in RANGE_TYPES:
                created += track_zone(
                    self.db, self.cfg, z, lo, hi, ts, observed, prev,
                    detected_at, entered_at,
                )
        for z in silent:
            created += track_zone(
                self.db, self.cfg, z, lo, hi, ts, observed, prev,
                detected_at, entered_at, silent=True,
            )
        return created

    # ------------------------------------------------------------------
    # replay
    # ------------------------------------------------------------------

    def replay_instrument(
        self, instrument_id: int, start_ms: Optional[int] = None,
        timeframes: Optional[set] = None,
    ) -> list[Event]:
        """Воспроизведение истории инструмента (§11).

        Свечи всех ТФ в хронологическом порядке close_time; для каждой сначала
        on_price-логика по диапазону [low,high] с observed=False (occurred_at —
        граница свечи, detected_at — текущее время, delayed при существенном
        отставании), затем структурные обновления on_closed_candle.
        timeframes — необязательное ограничение ТФ (воркер применяет настройку
        scan_timeframes); None — все ТФ из БД.

        Списки свечей читаются из БД один раз на ТФ; per-candle пересчёт
        получает нарастающий in-memory срез (префикс до start_ms + обработанные
        свечи) — то же содержимое, что давал бы db.get_candles на каждой свече,
        без per-candle N+1. Записи прогона — одним batch (один commit на replay
        вместо commit'а на каждую запись; живой путь вне replay не в батче).
        """
        tf_candles: dict[str, list[Candle]] = {}
        all_candles: list[Candle] = []
        for tf in TIMEFRAME_MINUTES:
            if timeframes is not None and tf not in timeframes:
                continue
            full = self.db.get_candles(instrument_id, tf)
            tf_candles[tf] = full
            if start_ms is None:
                all_candles += full
            else:
                all_candles += [c for c in full if c.open_time >= start_ms]
        all_candles.sort(key=lambda c: (c.close_time, TIMEFRAME_MINUTES[c.timeframe]))

        created: list[Event] = []
        now = now_ms()
        self._last_price.pop(instrument_id, None)
        last_ts: Optional[int] = None
        # нарастающий срез по ТФ: свечи до окна replay (уже в БД) + обработанные
        seen: dict[str, list[Candle]] = {
            tf: [c for c in full if start_ms is not None and c.open_time < start_ms]
            for tf, full in tf_candles.items()
        }
        with self.db.batch_writes():
            for c in all_candles:
                boundary = close_boundary_ms(c.open_time, c.timeframe)
                prev = self._last_price.get(instrument_id)
                created += self._track_all(
                    instrument_id, c.low, c.high, boundary, False, prev, now,
                    entered_at=c.open_time, timeframes=timeframes,
                )
                seen[c.timeframe].append(c)
                created += self.on_closed_candle(c, candles=seen[c.timeframe])
                self._last_price[instrument_id] = c.close
                last_ts = boundary

            # §8: если после обработки истории цена уже в актуальной зоне — отметить
            price = self._last_price.get(instrument_id)
            if price is not None and last_ts is not None:
                for z in self.db.get_zones(instrument_id, statuses=TRACKED_STATUSES):
                    if z.type not in RANGE_TYPES or z.is_level:
                        continue
                    if timeframes is not None and z.timeframe not in timeframes:
                        continue
                    if z.confirmed_at is None or z.confirmed_at > last_ts:
                        continue
                    if z.market_validity != "active":
                        continue
                    # ТЗ §4: OB до выхода цены из базы «касания» не имеет —
                    # цена внутри диапазона базы/импульса не является тестом
                    if is_ob_like(z) and z.evidence.get("phase") != "departed":
                        continue
                    # ТЗ 06.10.2026 §3.1: испорченное состояние выхода не
                    # порождает сигналов (T02: evidence_incomplete)
                    if z.evidence.get("integrity") == "inconsistent":
                        continue
                    if z.lower <= price <= z.upper:
                        ev = Event(
                            id=None, zone_id=z.id, cycle_id=z.cycle_id,
                            kind=EventKind.ALREADY_IN_ZONE, occurred_at=last_ts,
                            detected_at=now, price=price,
                            delayed=is_delayed(self.cfg, last_ts, now),
                            evidence={"note": "цена уже в зоне после обработки истории (§8)"},
                        )
                        ev = self._emit(ev)
                        if ev is not None:
                            created.append(ev)
        return created

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _emit(self, ev: Event) -> Optional[Event]:
        """Вставка события с пред-проверкой по ключу идемпотентности.

        db.insert_event при проигнорированном INSERT OR IGNORE может вернуть
        устаревший lastrowid (контрактный db.py не меняем) — сначала проверяем
        точечным запросом, что события с таким (zone, cycle, kind, occurred_at)
        ещё нет.
        """
        if self.db.has_event(ev.zone_id, ev.cycle_id, ev.kind, ev.occurred_at):
            return None
        if self.db.insert_event(ev) is not None:
            return ev
        return None

    @staticmethod
    def _visible(zone: Zone, tf: str, boundary: int) -> bool:
        """Зона того же ТФ и уже подтверждена к моменту boundary."""
        return (
            zone.timeframe == tf
            and zone.confirmed_at is not None
            and zone.confirmed_at <= boundary
        )

    def _insert_zone(self, z: Zone) -> Optional[int]:
        """Идемпотентная вставка зоны с проверкой возвращённого id.

        Сначала точечный поиск по дедуп-ключу: на replay каждая свеча
        заново находит те же зоны, и INSERT OR IGNORE + commit на каждый
        дубликат — основная стоимость прогона. db.insert_zone при
        проигнорированном INSERT OR IGNORE может вернуть устаревший
        lastrowid sqlite3 (контрактный db.py не меняем) — перепроверяем
        по дедуп-ключу и при несовпадении ищем существующую зону явно.
        """
        existing = self.db.find_zone_id(z)
        if existing is not None:
            return existing
        zid = self.db.insert_zone(z)
        if zid is not None:
            row = self.db.get_zone(zid)
            if (
                row is not None
                and row.type == z.type
                and row.direction == z.direction
                and row.timeframe == z.timeframe
                and row.lower == z.lower
                and row.upper == z.upper
                and row.formed_at == z.formed_at
                and row.cycle_id == z.cycle_id
            ):
                return zid
        return self.db.find_zone_id(z)

    def _update_evidence(self, zone: Zone, evidence: dict) -> None:
        # db.update_zone пишет evidence без source_candles — сохраняем их в json,
        # чтобы db._to_zone при чтении восстановил zone.source_candles
        evidence = dict(evidence)
        evidence["source_candles"] = zone.source_candles
        self.db.update_zone(zone.id, evidence=evidence)
