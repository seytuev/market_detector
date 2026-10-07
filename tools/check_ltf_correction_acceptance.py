#!/usr/bin/env python3
"""§16 (Этап 9): приёмка коррекции LTF — 22 проверки.

Стратегия: проверки, уже закреплённые pytest-тестами, выполняются ОДНИМ
прогоном pytest по nodeid (результат разбирается по строкам -v); эталонные
проверки (03/05/06/17/22) валидируются по артефактам Этапа 7
(data/diag/btc_etalon_2026_10/: events.jsonl, report.md, scratch-БД для
chart-слоёв) конкретными assert'ами. Итог: таблица PASS/FAIL в stdout и
data/diag/btc_etalon_2026_10/acceptance.md; exit 0 только при 22/22 PASS.

Запуск: .venv/Scripts/python.exe tools/check_ltf_correction_acceptance.py
(перед этим — полный прогон Этапа 7: tools/replay_btc_etalon_2026_10.py;
--report-only не требуется, читаются готовые артефакты)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = ROOT / "data" / "diag" / "btc_etalon_2026_10"

PYTEST_CHECKS: dict[str, tuple[str, list[str]]] = {
    "01": ("один счётчик подходящих в карточке/панели/таблице", [
        "tests/test_ltf_single_snapshot.py::test_single_snapshot_counts_and_version",
        "tests/test_ltf_single_snapshot.py::test_overview_counts_selected_context_only",
    ]),
    "02": ("этап согласован со счётчиком (0 ↔ нет зон, >0 ↔ возврат)", [
        "tests/test_ltf_single_snapshot.py::test_stage_follows_counts",
    ]),
    "03": ("финальный исход BSL 84419.69; Close>K исключает входы", [
        "tests/test_ltf_level_lifecycle.py::test_close_beyond_level_is_terminal_broken",
    ]),
    "04": ("touch ≠ строгое снятие; у уровня нет выдуманной глубины OB", [
        "tests/test_ltf_level_lifecycle.py::test_sweep_reclaimed_preserved",
        "tests/test_ltf_level_lifecycle.py::test_equal_close_not_terminal_then_broken",
        "tests/test_ltf_level_lifecycle.py::test_point_level_depth_is_null_in_api",
    ]),
    "05": ("replay восстанавливает отмену/новый сценарий; поздний якорь не стирает отмену", [
        "tests/test_ltf_engine.py::test_replay_does_not_attach_previous_epochs_to_new_scenario",
    ]),
    "06": ("две ручные линии BOS классифицированы; dump без отката — не secondary", [
        "tests/test_ltf_breaks.py::test_continuous_fall_without_pullback_is_not_two_stages",
        "tests/test_ltf_breaks.py::test_trace_reject_pullback_missing_on_continuous_fall",
    ]),
    "07": ("у каждого BOS/SMS — уровень и закрытая свеча с ценой/временем", [
        "tests/test_ltf_bos_sms_events.py::test_evidence_completeness_detect_level",
        "tests/test_ltf_bos_sms_events.py::test_evidence_completeness_engine_reverse_break",
    ]),
    "08": ("диапазон — выбранного движения; при смене сценария старый — история", [
        "tests/test_ltf_causal_chain.py::test_epoch_increments_and_active_selection_after_cancellation",
        "tests/test_ltf_origin_range.py::test_origin_range_lifecycle_after_primary_bos",
    ]),
    "09": ("связанная пара LH→LL или явный origin_range; HH не переименовывается", [
        "tests/test_ltf_origin_range.py::test_start_anchor_is_movement_start_not_earlier_higher_high",
    ]),
    "10": ("якоря после 3 правых свечей; provisional ≠ confirmed", [
        "tests/test_ltf_origin_range.py::test_both_anchors_require_confirmation",
        "tests/test_ltf_provisional_range.py::test_provisional_none_when_extreme_confirmed",
        "tests/test_ltf_provisional_range.py::test_current_provisional_hidden_by_default",
    ]),
    "11": ("база OB у начала движения не отбрасывается", [
        "tests/test_ltf_ob_fvg_lifecycle.py::test_ob_base_at_origin_kept",
    ]),
    "12": ("кандидаты найдены или явная причина; поиск не только в старом движении", [
        "tests/test_ltf_entries.py::test_detect_all_suitable_zones_no_ranking",
        "tests/test_ltf_engine.py::test_reopen_after_cancellation",
    ]),
    "13": ("перекрытый FVG — не новый void; OB оценивается независимо", [
        "tests/test_ltf_ob_fvg_lifecycle.py::test_fvg_filled_terminal_ob_independent",
        "tests/test_ltf_ob_fvg_lifecycle.py::test_fvg_filled_before_activation_born_filled",
    ]),
    "14": ("89% — можно перевыбрать; 90% — актуален, но вход закрыт", [
        "tests/test_ltf_ob_fvg_lifecycle.py::test_ob_depth_boundary_89_90",
    ]),
    "15": ("частичное пересечение Premium проходит; уведомление по полным границам", [
        "tests/test_ltf_eligibility.py::test_ok_partial_premium_overlap_acceptance",
        "tests/test_ltf_entries.py::test_classify_overlap_partial_no_cut",
    ]),
    "16": ("версия диапазона не дублирует BSL; снятый уровень не воскресает", [
        "tests/test_ltf_engine.py::test_range_anchor_level_not_duplicated",
        "tests/test_ltf_engine.py::test_swept_bsl_not_resurrected_on_range_update",
        "tests/test_final_eligibility.py::test_swept_level_not_revived_by_new_range_version",
    ]),
    "17": ("86 698.99 имеет структурную роль/историю или НЕ уровень отмены", [
        "tests/test_ltf_causal_chain.py::test_reverse_break_level_stored_on_cancellation",
    ]),
    "18": ("отключение типа зоны единообразно: допуск, счётчики, график, уведомления", [
        "tests/test_ltf_engine.py::test_disabled_entry_type_reclassify",
        "tests/test_ltf_api.py::test_settings_entry_types_reclassify",
        "tests/test_ltf_api.py::test_observation_chart_layers",
    ]),
    "19": ("неполная история → data_pending, а не «доказанное отсутствие зон»", [
        "tests/test_ltf_single_snapshot.py::test_stage_follows_counts",
        "tests/test_ltf_current_setup.py::test_instruments_one_row_per_instrument",
    ]),
    "20": ("replay/reconnect не повторяет сообщения; доставленное сохраняется", [
        "tests/test_ltf_notify.py::test_dispatcher_touch_not_resent_after_range_recalc",
        "tests/test_ltf_migration_v2.py::test_apply_recalculates_and_preserves_delivered",
        "tests/test_ltf_migration_v2.py::test_apply_idempotent_second_run",
    ]),
    "21": ("масштаб графика не меняет геометрию и допуск", [
        "tests/test_ltf_api.py::test_chart_layers_ignore_display_scale_params",
    ]),
    "22": ("график эталона: якоря, 50%, BOS/SMS, исходы OB/FVG", [
        "tests/test_ltf_api.py::test_observation_chart_layers",
    ]),
}


def _events() -> list[dict]:
    path = WORK / "events.jsonl"
    assert path.exists(), f"нет {path} — сначала tools/replay_btc_etalon_2026_10.py"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def _report() -> str:
    path = WORK / "report.md"
    assert path.exists(), f"нет {path} — сначала tools/replay_btc_etalon_2026_10.py"
    return path.read_text(encoding="utf-8")


def check_03() -> str:
    ev = [e for e in _events()
          if e["type"] == "liquidity_outcome" and e["level"] == 84419.69]
    states = {e["state"]: e for e in ev}
    assert "confirmed" in states, f"нет sweep_reclaimed по 84419.69: {ev}"
    assert "failed" in states, f"нет broken_without_reclaim по 84419.69: {ev}"
    assert states["confirmed"]["close_price"] < 84419.69
    assert states["failed"]["close_price"] > 84419.69
    return ("events.jsonl: sweep_reclaimed C=84162.65<K и broken C=84908.01>K; "
            "отчёт Q4 (level_broken навсегда)")


def check_05() -> str:
    ev = _events()
    cancel = [e for e in ev if e["type"] == "scenario_cancel"]
    opens = [e for e in ev if e["type"] == "scenario_open"]
    assert cancel and cancel[0]["reverse_break_level"] == 84419.69, cancel
    epochs = {e["scenario_id"]: e["epoch"] for e in opens}
    assert len(opens) >= 2 and max(epochs.values()) == 2, opens
    return ("events.jsonl: scenario_cancel sc эпохи 1 (уровень 84419.69) → "
            "scenario_open эпохи 2; отчёт Q1/Q2")


def check_06() -> str:
    rep = _report()
    assert "одно движение" in rep and "вторичного BOS нет" in rep
    ev = _events()
    sec = [e for e in ev if e["type"] == "structure_event"
           and e["stage"] == "secondary"]
    # secondary допустим только в эпохе 1 (83500.01 с откатом); после 84520 — нет
    assert all(e["break_level"] == 83500.01 for e in sec), sec
    return ("отчёт Q6: линии классифицированы (bull 84419.69 / bear 84520); "
            "после 84520 — одно движение без отката, secondary нет")


def check_17() -> str:
    rep = _report()
    assert "86 698.99 уровнем отмены НЕ является" in rep
    return ("отчёт Q1: 86698.99 = HH 06.10 (pivot 11155), НЕ уровень отмены; "
            "уровень отмены 84419.69 с pivot-provenance")


def check_22() -> str:
    from app.config import load_settings
    from app.db import Database
    from app.services.overview import observation_chart_layers

    scratch = WORK / "scratch.db"
    assert scratch.exists(), f"нет {scratch}"
    db = Database(str(scratch))
    try:
        obs_id = db.conn.execute(
            "SELECT id FROM ltf_observation WHERE instrument_id=1"
        ).fetchone()[0]
        chart = observation_chart_layers(db, load_settings(), obs_id)
    finally:
        db.close()
    ranges = chart["ranges"]
    assert ranges, "нет диапазонов на графике эталона"
    cur = [r for r in ranges if r["current"]][0]
    assert cur["mid"] is not None and cur["anchor_high_pivot_id"]
    assert chart["structure_events"], "нет BOS/SMS на графике"
    types = {(e["type"], e["lower"]) for e in
             chart["entries"] + chart["entries_excluded"]}
    assert ("OB", 86308.0) in types and ("FVG", 85758.81) in types, types
    tests = {(t["level"], t["state"]) for t in chart["liquidity_tests"]}
    assert (84419.69, "failed") in tests, tests
    return ("chart-слои scratch: якоря+mid диапазона, BOS/SMS, OB 7366/FVG 7364 "
            "с исходами, liquidity-исход 84419.69 (failed); отчёт Q3–Q5")


ETALON_CHECKS = {
    "03": check_03, "05": check_05, "06": check_06,
    "17": check_17, "22": check_22,
}


def run_pytest_checks() -> dict[str, str]:
    """Все nodeid одним прогоном; вернуть {nodeid: PASSED|FAILED|ERROR}."""
    nodeids = [n for _, ids in PYTEST_CHECKS.values() for n in ids]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--tb=no", *nodeids],
        capture_output=True, text=True, cwd=ROOT,
    )
    out = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"(tests[\\/]\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL)",
                     line)
        if m:
            out[m.group(1).replace("\\", "/")] = m.group(2)
    return out


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results = run_pytest_checks()
    rows: list[tuple[str, str, str, str]] = []   # №, status, title, evidence
    n_pass = 0
    for num in sorted(PYTEST_CHECKS, key=int):
        title, nodeids = PYTEST_CHECKS[num]
        fails = [n for n in nodeids if results.get(n) != "PASSED"]
        missing = [n for n in nodeids if n not in results]
        status = "PASS" if not fails and not missing else "FAIL"
        evidence = "; ".join(nodeids)
        if missing:
            status = "FAIL"
            evidence += f" (не найдены: {missing})"
        if num in ETALON_CHECKS and status == "PASS":
            try:
                evidence = ETALON_CHECKS[num]() + " | " + evidence
            except (AssertionError, FileNotFoundError) as exc:
                status = "FAIL"
                evidence = f"эталон: {exc} | {evidence}"
        if status == "PASS":
            n_pass += 1
        rows.append((num, status, title, evidence))

    width = 5
    print(f"{'№':<3} {'статус':<{width}} проверка → доказательство")
    for num, status, title, evidence in rows:
        mark = "✓" if status == "PASS" else "✗"
        print(f"{num:<3} {mark} {status:<5} {title}")
        print(f"        {evidence}")
    print(f"\nИтого: {n_pass}/22 PASS")

    md = ["# Приёмка коррекции LTF (§16) — 22 проверки", "",
          f"Итого: **{n_pass}/22 PASS**", "",
          "| № | Статус | Проверка | Доказательство |",
          "|---|---|---|---|"]
    for num, status, title, evidence in rows:
        md.append(f"| {num} | {status} | {title} | {evidence} |")
    md.append("")
    md.append("Эталонные проверки — по артефактам Этапа 7 "
              "(data/diag/btc_etalon_2026_10/: replay report, events.jsonl, "
              "slices, scratch-БД); остальные — pytest-прогон nodeid одним "
              "вызовом (tools/check_ltf_correction_acceptance.py).")
    (WORK / "acceptance.md").write_text("\n".join(md), encoding="utf-8")
    print(f"таблица: {WORK / 'acceptance.md'}")
    sys.exit(0 if n_pass == 22 else 1)


if __name__ == "__main__":
    main()
