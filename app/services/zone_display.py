"""Read-only chart fill projection; visit facts never rewrite trading state."""
from ..models import ZoneType


def fill_depth(zone, visit_depth=0):
    return max(0.0, min(1.0, max(float(zone.max_test_depth), float(visit_depth))))


def fvg_fill_depths(db, zones):
    """One aggregate per batch, including legacy FVGs whose OB stats are zero."""
    fvgs = {z.id: z for z in zones if z.id is not None and z.type == ZoneType.FVG}
    depths = {zid: fill_depth(z) for zid, z in fvgs.items()}
    ids = list(fvgs)
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ','.join('?' for _ in batch)
        rows = db.conn.execute(
            f"SELECT zone_id, cycle_id, MAX(max_depth) AS depth FROM visit "
            f"WHERE zone_id IN ({marks}) GROUP BY zone_id, cycle_id", batch,
        ).fetchall()
        for row in rows:
            zone = fvgs[row['zone_id']]
            if row['cycle_id'] == zone.cycle_id:
                depths[zone.id] = fill_depth(zone, row['depth'])
    return depths
