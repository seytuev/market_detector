"""ТЗ «LTF Current Setup» §10: модель пригодности eligible_now.

- стабильные reason-коды и их приоритет;
- частичное пересечение нужной половины достаточно (приёмка п.16);
- midpoint принадлежит обеим половинам (явный convention §10);
- OB с тестом 20% подходит, с тестом 90% — нет (приёмка п.13);
- снятый BSL/SSL, отключённый тип, invalid, неразрешённое происхождение;
- fallback reason из state для строк до миграции.
"""
from __future__ import annotations

from app.config import DetectorConfig
from app.engine.ltf.eligibility import (
    ELIGIBILITY_REASONS,
    REASON_INVALID,
    REASON_OK,
    REASON_ORIGIN_UNRESOLVED,
    REASON_OUTSIDE_PD,
    REASON_RANGE_PENDING,
    REASON_LEVEL_BROKEN,
    REASON_SWEPT_LEVEL,
    REASON_TESTED_TOO_DEEP,
    REASON_TYPE_DISABLED,
    entry_reason,
    evaluate_entry,
)
from app.engine.ltf.ranges import RangeDraft
from app.models import Direction
from app.models_ltf import (
    LtfEntryZone,
    LtfLiquidityTest,
    LtfMovement,
    LtfScenarioEntry,
)

T0 = 1_780_000_000_000


def _rng(direction: Direction = Direction.BEAR,
         lower: float = 90.0, upper: float = 110.0) -> RangeDraft:
    return RangeDraft(
        direction=direction, lower=lower, upper=upper, mid=(lower + upper) / 2,
        anchor_low_ref=None, anchor_high_ref=None, available_at=T0,
    )


def _zone(
    type_: str = "OB", direction: Direction = Direction.BEAR,
    lower: float = 98.0, upper: float = 102.0, validity: str = "fresh",
    depth: float = 0.0, extreme=None, movement_id: int = 0,
) -> LtfEntryZone:
    return LtfEntryZone(
        id=1, instrument_id=1, type=type_, direction=direction,
        lower=lower, upper=upper, formed_at=T0, confirmed_at=T0 + 1000,
        validity=validity, max_test_depth=depth, test_extreme=extreme,
        movement_id=movement_id,
    )


def _movement(mid: int = 1, provenance: str = "ok") -> LtfMovement:
    return LtfMovement(
        id=mid, scenario_id=1, start_pivot_id=1, end_pivot_id=2,
        start_at=T0, end_at=T0 + 100, provenance_status=provenance,
    )


def _test(entry_zone_id: int, state: str) -> LtfLiquidityTest:
    return LtfLiquidityTest(
        id=1, entry_zone_id=entry_zone_id, scenario_id=1, level=100.0,
        touch_at=T0, candle_open_time=T0, state=state,
    )


def test_reason_codes_stable():
    assert ELIGIBILITY_REASONS == (
        "ok", "outside_pd", "tested_too_deep", "type_disabled", "invalid",
        "swept_level", "level_broken", "fvg_filled", "origin_unresolved",
        "range_pending",
    )


def test_ok_partial_premium_overlap_acceptance():
    """п.16: частично пересекающий Premium OB проходит фильтр — eligible
    определяется пересечением, границы не обрезаются."""
    cfg = DetectorConfig()
    rng = _rng()  # Premium [100; 110]
    ev = evaluate_entry(_zone("OB", lower=98.0, upper=102.0),
                        Direction.BEAR, cfg, rng)
    assert (ev.reason, ev.state) == (REASON_OK, "fresh")
    assert ev.eligible is True and ev.overlap == "partial"
    # зона, касающаяся Premium ровно границей M — пересечение есть
    ev2 = evaluate_entry(_zone("OB", lower=95.0, upper=100.0),
                         Direction.BEAR, cfg, rng)
    assert ev2.reason == REASON_OK and ev2.overlap == "partial"


def test_midpoint_belongs_to_both_halves():
    """Явный convention §10: уровень ровно на midpoint входит в обе
    половины; направление сценария определяет список."""
    cfg = DetectorConfig()
    rng = _rng()  # M = 100
    # BSL на M: для bear (Premium [100;110]) — подходит
    ev = evaluate_entry(_zone("BSL", lower=100.0, upper=100.0),
                        Direction.BEAR, cfg, rng)
    assert ev.reason == REASON_OK and ev.state == "fresh"
    # и для bull (Discount [90;100]) — тоже подходит
    ev2 = evaluate_entry(_zone("BSL", Direction.BULL, 100.0, 100.0),
                         Direction.BULL, cfg, rng)
    assert ev2.reason == REASON_OK
    # зона ровно от M до M не «вне диапазона» ни для одной стороны
    ev3 = evaluate_entry(_zone("FVG", lower=100.0, upper=100.0),
                         Direction.BEAR, cfg, rng)
    assert ev3.reason == REASON_OK


def test_outside_pd():
    cfg = DetectorConfig()
    ev = evaluate_entry(_zone("OB", lower=80.0, upper=90.0),
                        Direction.BEAR, cfg, _rng())
    assert (ev.reason, ev.state) == (REASON_OUTSIDE_PD, "out_of_range")
    assert ev.eligible is False and ev.overlap == "none"


def test_invalid_never_back_to_fresh():
    """invalid не переводится обратно в fresh, даже при отключённом типе
    (рыночный факт приоритетнее конфигурации)."""
    cfg = DetectorConfig()
    ev = evaluate_entry(_zone(validity="invalid"), Direction.BEAR, cfg, _rng())
    assert (ev.reason, ev.state) == (REASON_INVALID, "invalid")
    cfg.ltf_entry_types = "FVG"  # OB отключён — reason остаётся invalid
    ev2 = evaluate_entry(_zone(validity="invalid"), Direction.BEAR, cfg, _rng())
    assert ev2.reason == REASON_INVALID


def test_type_disabled():
    """§10: тип вне ltf_entry_types не подходит; зона и история сохраняются."""
    cfg = DetectorConfig()
    cfg.ltf_entry_types = "OB,BSL,SSL"
    ev = evaluate_entry(_zone("FVG", lower=101.0, upper=103.0),
                        Direction.BEAR, cfg, _rng())
    assert (ev.reason, ev.state) == (REASON_TYPE_DISABLED, "out_of_range")
    # включённый тип проходит
    cfg.ltf_entry_types = "FVG,OB,BSL,SSL"
    ev2 = evaluate_entry(_zone("FVG", lower=101.0, upper=103.0),
                         Direction.BEAR, cfg, _rng())
    assert ev2.reason == REASON_OK
    # регистр/пробелы в настройке не влияют
    cfg.ltf_entry_types = " ob , fvg"
    assert cfg.ltf_entry_type_set() == {"OB", "FVG"}


def test_swept_level_not_eligible():
    """п.17: подтверждённый sweep закрывает пригодность уровня; §9 (Этап 5):
    failed (строгое закрытие за уровнем) — тоже терминально, level_broken."""
    cfg = DetectorConfig()
    zone = _zone("BSL", lower=105.0, upper=105.0)
    ev = evaluate_entry(zone, Direction.BEAR, cfg, _rng(),
                        liquidity_tests=[_test(1, "confirmed")])
    assert (ev.reason, ev.state) == (REASON_SWEPT_LEVEL, "tested")
    ev2 = evaluate_entry(zone, Direction.BEAR, cfg, _rng(),
                         liquidity_tests=[_test(1, "failed")])
    assert (ev2.reason, ev2.state) == (REASON_LEVEL_BROKEN, "tested")
    # тест чужой зоны не влияет
    ev3 = evaluate_entry(zone, Direction.BEAR, cfg, _rng(),
                         liquidity_tests=[_test(999, "confirmed")])
    assert ev3.reason == REASON_OK


def test_origin_unresolved():
    """§10: movement_id зоны обязан разрешаться в движение сценария с
    однозначным происхождением; movement_id=0 (якорь диапазона) — вне
    проверки; movements не переданы — проверка не применяется."""
    cfg = DetectorConfig()
    rng = _rng()
    ok_zone = _zone(movement_id=1)
    ev = evaluate_entry(ok_zone, Direction.BEAR, cfg, rng,
                        movements=[_movement(1)])
    assert ev.reason == REASON_OK
    stranger = _zone(movement_id=42)
    ev2 = evaluate_entry(stranger, Direction.BEAR, cfg, rng,
                         movements=[_movement(1)])
    assert (ev2.reason, ev2.state) == (REASON_ORIGIN_UNRESOLVED, "out_of_range")
    ev3 = evaluate_entry(stranger, Direction.BEAR, cfg, rng, movements=None)
    assert ev3.reason == REASON_OK
    ev4 = evaluate_entry(ok_zone, Direction.BEAR, cfg, rng,
                         movements=[_movement(1, provenance="ambiguous")])
    assert ev4.reason == REASON_ORIGIN_UNRESOLVED


def test_ob_reuse_depth_acceptance():
    """п.13: OB с прежним тестом 20% допустим к повторному выбору; с тестом
    90% — рыночно актуален (не invalid), но новый вход запрещён."""
    cfg = DetectorConfig()
    rng = _rng()
    # медвежий OB [98;102]: P90 = 101.6
    shallow = _zone(validity="tested", depth=0.2, extreme=98.8)
    ev = evaluate_entry(shallow, Direction.BEAR, cfg, rng)
    assert (ev.reason, ev.state) == (REASON_OK, "fresh")
    deep = _zone(validity="tested", depth=0.9, extreme=101.6)
    ev2 = evaluate_entry(deep, Direction.BEAR, cfg, rng)
    assert (ev2.reason, ev2.state) == (REASON_TESTED_TOO_DEEP, "tested")
    # глубокий тест вне половины: outside_pd важнее для отчёта причин? Нет —
    # tested_too_deep проверяется до пространственного фильтра
    deep_out = _zone(lower=80.0, upper=85.0, validity="tested",
                     depth=0.95, extreme=84.75)
    ev3 = evaluate_entry(deep_out, Direction.BEAR, cfg, rng)
    assert ev3.reason == REASON_TESTED_TOO_DEEP


def test_range_pending_keeps_candidate():
    """Диапазона ещё нет: пространственный фильтр не применяется, зона —
    кандидат (state fresh, reason range_pending); отключённый тип и глубокий
    тест видны уже на этом этапе."""
    cfg = DetectorConfig()
    ev = evaluate_entry(_zone(), Direction.BEAR, cfg, None)
    assert (ev.reason, ev.state) == (REASON_RANGE_PENDING, "fresh")
    assert ev.eligible is True and ev.overlap == "pending"
    cfg2 = DetectorConfig()
    cfg2.ltf_entry_types = "OB"
    ev2 = evaluate_entry(_zone("FVG"), Direction.BEAR, cfg2, None)
    assert ev2.reason == REASON_TYPE_DISABLED
    deep = _zone(validity="tested", depth=0.9, extreme=101.6)
    ev3 = evaluate_entry(deep, Direction.BEAR, cfg, None)
    assert (ev3.reason, ev3.state) == (REASON_TESTED_TOO_DEEP, "tested")


def test_entry_reason_fallback_pre_migration():
    """Строки до миграции (reason == ''): пригодность выводится из state."""
    def se(state: str, reason: str = "") -> LtfScenarioEntry:
        return LtfScenarioEntry(
            id=1, scenario_id=1, entry_zone_id=1, range_version=1,
            state=state, reason=reason,
        )
    assert entry_reason(se("fresh")) == REASON_OK
    assert entry_reason(se("tested")) == ""
    assert entry_reason(se("out_of_range")) == REASON_OUTSIDE_PD
    assert entry_reason(se("invalid")) == REASON_INVALID
    # явный reason всегда побеждает
    assert entry_reason(se("fresh", REASON_TYPE_DISABLED)) == REASON_TYPE_DISABLED
