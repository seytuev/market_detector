"""Выбор актуального эпизода диапазона v2 (R-08, ТЗ 07.10.2026):
pure-тесты select_current_episode на ручных эпизодах — движок v2 ещё не
включён, помощник проверяется только здесь."""
from __future__ import annotations

from typing import Optional

from app.models_alt import AltEpisodeState, AltRangeEpisode
from app.services.alt_overview import (
    EPISODE_RELEVANCE_WINDOW_MS,
    select_current_episode,
)

DAY = 86_400_000
T0 = 1_780_000_000_000  # граница суток (кратна DAY)
AS_OF = T0 + 400 * DAY


def _ep(ep_id: int, state: str, *, base_start: int = T0,
        lower: float = 1.0, upper: float = 2.0,
        end_reason: Optional[str] = None,
        end_confirmed: Optional[int] = None,
        acc_end: Optional[int] = None,
        detected: int = 0) -> AltRangeEpisode:
    return AltRangeEpisode(
        id=ep_id, asset_id=1, source_id=1, origin_key=f"1:{base_start}:{ep_id}",
        anchor_start_open_time=base_start, base_start_open_time=base_start,
        lower=lower, upper=upper, width=upper - lower, mid=(lower + upper) / 2,
        state=state,
        base_end_open_time=end_confirmed, base_end_reason=end_reason,
        base_end_confirmed_at_ms=end_confirmed,
        accompaniment_end_open_time=acc_end,
        detected_at_ms=detected,
    )


def test_no_episodes() -> None:
    ep, reason, alts = select_current_episode([], 1.5, AS_OF)
    assert ep is None and reason == "no_episodes" and alts == []


def test_no_qualified_episode() -> None:
    """Формирующийся/завершённый эпизод не выбирается актуальным, даже если
    цена внутри его границ."""
    forming = _ep(1, AltEpisodeState.FORMING.value, lower=1.0, upper=2.0)
    terminal = _ep(2, AltEpisodeState.TERMINAL.value, base_start=T0 + DAY)
    ep, reason, alts = select_current_episode([forming, terminal], 1.5, AS_OF)
    assert ep is None and reason == "no_qualified_episode" and alts == []


def test_inside_base_preferred_over_accompaniment() -> None:
    """База с ценой внутри выигрывает у более свежего сопровождения после
    подтверждённого выхода."""
    inside = _ep(1, AltEpisodeState.MATURE.value, base_start=T0,
                 lower=1.0, upper=2.0)
    accomp = _ep(2, AltEpisodeState.ACCOMPANIMENT.value, base_start=T0 + 200 * DAY,
                 lower=3.0, upper=4.0, end_reason="breakout_confirmed",
                 end_confirmed=AS_OF - 5 * DAY)
    ep, reason, alts = select_current_episode([accomp, inside], 1.5, AS_OF)
    assert ep is inside and reason == "inside_base" and alts == [accomp]


def test_recent_breakout_accompaniment_selected() -> None:
    """Недавний подтверждённый выход с действующим сопровождением — актуален,
    даже когда цена уже выше базы."""
    accomp = _ep(1, AltEpisodeState.ACCOMPANIMENT.value, base_start=T0,
                 lower=1.0, upper=2.0, end_reason="breakout_confirmed",
                 end_confirmed=AS_OF - 10 * DAY)
    ep, reason, alts = select_current_episode([accomp], 5.0, AS_OF)
    assert ep is accomp and reason == "recent_breakout_accompaniment"
    assert alts == []


def test_accompaniment_ended_not_relevant() -> None:
    """Сопровождение завершено до as_of — эпизод исторический."""
    done = _ep(1, AltEpisodeState.ACCOMPANIMENT.value, base_start=T0,
               lower=1.0, upper=2.0, end_reason="breakout_confirmed",
               end_confirmed=AS_OF - 10 * DAY, acc_end=AS_OF - DAY)
    ep, reason, alts = select_current_episode([done], 5.0, AS_OF)
    assert ep is None and reason == "no_relevant_episode" and alts == []


def test_historical_only_returns_none_with_reason() -> None:
    """Старая база с выходом за пределами окна релевантности не подменяет
    актуальный диапазон (пример AAVE из ТЗ)."""
    historical = _ep(
        1, AltEpisodeState.ACCOMPANIMENT.value, base_start=T0,
        lower=1.0, upper=2.0, end_reason="breakout_confirmed",
        end_confirmed=AS_OF - EPISODE_RELEVANCE_WINDOW_MS - DAY,
    )
    ep, reason, alts = select_current_episode([historical], 5.0, AS_OF)
    assert ep is None and reason == "no_relevant_episode" and alts == []


def test_freshness_wins_among_inside_base() -> None:
    """Из двух баз с ценой внутри выбирается более свежая; вторая —
    в альтернативах."""
    older = _ep(1, AltEpisodeState.MATURE.value, base_start=T0,
                lower=1.0, upper=2.0)
    newer = _ep(2, AltEpisodeState.ACTIVE.value, base_start=T0 + 50 * DAY,
                lower=1.2, upper=1.8)
    ep, reason, alts = select_current_episode([older, newer], 1.5, AS_OF)
    assert ep is newer and reason == "inside_base" and alts == [older]


def test_confirmation_time_tiebreak_is_deterministic() -> None:
    """Равная свежесть → устойчивый порядок по времени подтверждения (R-08)."""
    early = _ep(1, AltEpisodeState.MATURE.value, base_start=T0,
                detected=T0 + 10 * DAY)
    late = _ep(2, AltEpisodeState.MATURE.value, base_start=T0,
               detected=T0 + 20 * DAY)
    ep, reason, alts = select_current_episode([early, late], 1.5, AS_OF)
    assert ep is late and reason == "inside_base" and alts == [early]
    # порядок входа не влияет на результат
    ep2, _, _ = select_current_episode([late, early], 1.5, AS_OF)
    assert ep2 is late


def test_ambiguous_when_parity() -> None:
    """Полный паритет свежести и времени подтверждения у двух баз с ценой
    внутри — статус неоднозначности, альтернативы перечислены устойчиво."""
    a = _ep(1, AltEpisodeState.MATURE.value, base_start=T0, detected=T0 + DAY)
    b = _ep(2, AltEpisodeState.MATURE.value, base_start=T0, detected=T0 + DAY)
    ep, reason, alts = select_current_episode([a, b], 1.5, AS_OF)
    assert ep is None and reason == "ambiguous"
    assert [x.id for x in alts] == [b.id, a.id]  # устойчивый порядок по ID
    ep2, reason2, alts2 = select_current_episode([b, a], 1.5, AS_OF)
    assert ep2 is None and reason2 == "ambiguous"
    assert [x.id for x in alts2] == [b.id, a.id]


def test_narrowness_is_not_an_advantage() -> None:
    """Узкая старая база не выигрывает у более свежей широкой (R-08)."""
    narrow_old = _ep(1, AltEpisodeState.MATURE.value, base_start=T0,
                     lower=1.4, upper=1.6)
    wide_new = _ep(2, AltEpisodeState.MATURE.value, base_start=T0 + 100 * DAY,
                   lower=1.0, upper=2.0)
    ep, reason, _ = select_current_episode([narrow_old, wide_new], 1.5, AS_OF)
    assert ep is wide_new and reason == "inside_base"


def test_last_close_none_uses_accompaniment_only() -> None:
    """Без текущей цены «внутри базы» недоступно; сопровождение остаётся."""
    accomp = _ep(1, AltEpisodeState.ACCOMPANIMENT.value, base_start=T0,
                 lower=1.0, upper=2.0, end_reason="breakout_confirmed",
                 end_confirmed=AS_OF - 3 * DAY)
    ep, reason, _ = select_current_episode([accomp], None, AS_OF)
    assert ep is accomp and reason == "recent_breakout_accompaniment"
    mature = _ep(2, AltEpisodeState.MATURE.value, base_start=T0 + DAY)
    ep2, reason2, _ = select_current_episode([mature], None, AS_OF)
    assert ep2 is None and reason2 == "no_relevant_episode"
