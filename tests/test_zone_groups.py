"""Визуальное объединение пересекающихся зон (§10): общий модуль группировки.

Объединение только визуальное и для текста уведомлений — исходные зоны,
их границы, середины и правила уведомлений не меняются.
"""
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.services.zone_groups import group_members_by_zone, union_find_groups


def _zone(zid: int, lower: float, upper: float,
          ztype: ZoneType = ZoneType.OB) -> Zone:
    return Zone(zid, 1, ztype, Direction.BULL, "D1", lower=lower, upper=upper,
                formed_at=0, confirmed_at=0, status=ZoneStatus.ACTIVE,
                created_at=0)


def test_overlapping_zones_grouped_and_members_listed():
    zones = [_zone(1, 100, 200), _zone(2, 150, 250, ZoneType.OB),
             _zone(3, 500, 600)]
    members = group_members_by_zone(zones)
    assert set(members) == {1, 2}
    assert [z.id for z in members[1]] == [2]
    assert [z.id for z in members[2]] == [1]


def test_chain_merge_through_transitive_overlap():
    # касание границ = пересечение; цепочка 1–2–3 сливается в одну группу
    zones = [_zone(1, 100, 200), _zone(2, 200, 300), _zone(3, 250, 400)]
    groups = union_find_groups(zones)
    assert len(groups) == 1
    assert {z.id for z in groups[0]} == {1, 2, 3}


def test_single_zones_have_no_group():
    zones = [_zone(1, 100, 200), _zone(2, 300, 400)]
    assert group_members_by_zone(zones) == {}


def test_different_types_never_merge():
    # OB пересекается с FVG — не сливаются; второй OB, касающийся только
    # через FVG, тоже не попадает в группу (цепочка рвётся на типе)
    zones = [_zone(1, 100, 200, ZoneType.OB),
             _zone(2, 150, 220, ZoneType.FVG),
             _zone(3, 210, 300, ZoneType.OB)]
    assert group_members_by_zone(zones) == {}


def test_same_type_chain_still_merges():
    # однотипная цепочка сливается транзитивно, даже если крайние зоны
    # напрямую не пересекаются
    zones = [_zone(1, 100, 200), _zone(2, 190, 260), _zone(3, 250, 400)]
    members = group_members_by_zone(zones)
    assert set(members) == {1, 2, 3}
