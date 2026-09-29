"""LTF Confirmations: детекторы структуры H1 (спека LTF_Confirmations_Window_Spec_v0.2).

Чистые функции над списками Candle (pivots/roles/breaks/ranges) и
материализация в БД (structure.sync_structure).
"""
from .breaks import (
    CancellationSignal,
    StructureEventDraft,
    StructureScanResult,
    bear_break,
    bull_break,
    detect_breaks,
)
from .pivots import PivotCandidate, confirmed_pivots, find_h1_pivots
from .ranges import (
    RANGE_PIVOT_LEFT,
    RANGE_PIVOT_RIGHT,
    RangeDraft,
    current_range,
    eligible_overlap,
    level_in_half,
    provisional_range,
    range_recalc,
    target_half,
)
from .roles import RoleChange, RoleResult, assign_roles
from .structure import StructureSyncResult, sync_structure
from .engine import LtfEngine, LtfTickResult
from .eligibility import (
    ELIGIBILITY_REASONS,
    REASON_INVALID,
    REASON_OK,
    REASON_ORIGIN_UNRESOLVED,
    REASON_OUTSIDE_PD,
    REASON_RANGE_PENDING,
    REASON_SWEPT_LEVEL,
    REASON_TESTED_TOO_DEEP,
    REASON_TYPE_DISABLED,
    EligibilityResult,
    entry_reason,
    evaluate_entry,
)
from .entries import (
    EntryDetection,
    EntryZoneDraft,
    MovementDraft,
    build_movement,
    classify_entry,
    detect_entry_zones,
    entry_reusable,
    test_depth_of,
    touch_bar,
)
from .liquidity import resolve_sweep

__all__ = [
    "ELIGIBILITY_REASONS",
    "EntryDetection",
    "EntryZoneDraft",
    "EligibilityResult",
    "LtfEngine",
    "LtfTickResult",
    "MovementDraft",
    "CancellationSignal",
    "PivotCandidate",
    "RANGE_PIVOT_LEFT",
    "RANGE_PIVOT_RIGHT",
    "REASON_INVALID",
    "REASON_OK",
    "REASON_ORIGIN_UNRESOLVED",
    "REASON_OUTSIDE_PD",
    "REASON_RANGE_PENDING",
    "REASON_SWEPT_LEVEL",
    "REASON_TESTED_TOO_DEEP",
    "REASON_TYPE_DISABLED",
    "RangeDraft",
    "RoleChange",
    "RoleResult",
    "StructureEventDraft",
    "StructureScanResult",
    "StructureSyncResult",
    "assign_roles",
    "bear_break",
    "build_movement",
    "bull_break",
    "classify_entry",
    "confirmed_pivots",
    "current_range",
    "detect_breaks",
    "detect_entry_zones",
    "eligible_overlap",
    "entry_reason",
    "entry_reusable",
    "evaluate_entry",
    "find_h1_pivots",
    "level_in_half",
    "provisional_range",
    "range_recalc",
    "resolve_sweep",
    "sync_structure",
    "target_half",
    "test_depth_of",
    "touch_bar",
]
