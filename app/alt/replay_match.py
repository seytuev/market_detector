"""Сопоставление эпизода v2 с разметкой (A05).

Цена и время — разные условия. Сдвиг на год при тех же границах не
совпадение. Незрелый decayed не считается опубликованной базой.
"""
from __future__ import annotations

from typing import Any, Optional

from ..models_alt import AltEpisodeState

PUBLISHED_STATES = frozenset({
    AltEpisodeState.MATURE.value,
    AltEpisodeState.ACCOMPANIMENT.value,
    AltEpisodeState.TERMINAL.value,
})
# Распад уже после выхода из базы — опубликованный эпизод. Распад
# формирующегося кандидата этими видами не отмечен.
MATURE_LIFECYCLE = frozenset({
    "breakout", "retest", "target_hit", "targets_completed",
})


def published_episode(episode: dict[str, Any]) -> bool:
    """Эпизод можно сравнивать с размеченной зрелой базой."""
    state = episode.get("state")
    if state in PUBLISHED_STATES:
        return True
    if state == AltEpisodeState.DECAYED.value:
        kinds = set(episode.get("lifecycle_kinds") or ())
        return bool(kinds & MATURE_LIFECYCLE)
    return False


def intervals_overlap(
    left_start: Optional[int], left_end: Optional[int],
    right_start: Optional[int], right_end: Optional[int],
) -> bool:
    if left_start is None or right_start is None:
        return False
    left_stop = left_start if left_end is None else left_end
    right_stop = right_start if right_end is None else right_end
    return left_start <= right_stop and right_start <= left_stop


def selection_matches(
    selected_id: Optional[int], matched_episode_id: Optional[int],
) -> bool:
    """Главная карточка верна только когда выбран именно совпавший эпизод."""
    return (
        selected_id is not None
        and matched_episode_id is not None
        and selected_id == matched_episode_id
    )
