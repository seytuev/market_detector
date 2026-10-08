"""Измеритель alt replay (A05): время отдельно от цены, незрелый decayed,
неверный выбранный эпизод."""
from app.alt.replay_match import (
    intervals_overlap,
    published_episode,
    selection_matches,
)


def test_same_prices_a_year_apart_do_not_overlap():
    year = 365 * 86_400_000
    start = 1_767_225_600_000
    assert intervals_overlap(start, start + 30 * 86_400_000, start, start + year)
    assert not intervals_overlap(
        start, start + 40 * 86_400_000,
        start + year, start + year + 40 * 86_400_000,
    )


def test_immature_decayed_is_not_a_published_base():
    assert published_episode({"state": "mature"}) is True
    assert published_episode({
        "state": "decayed", "lifecycle_kinds": ["breakout"],
    }) is True
    assert published_episode({
        "state": "decayed", "lifecycle_kinds": [],
    }) is False


def test_wrong_selected_episode_fails_even_if_a_candidate_matched():
    assert selection_matches(5, 5) is True
    assert selection_matches(9, 5) is False
    assert selection_matches(None, 5) is False
