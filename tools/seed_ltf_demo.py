"""Засев демо-БД для живой проверки окна LTF (tools/seed_ltf_demo.py).

Создаёт временную БД с синтетической серией H1 (сценарий серии H из
tests/test_ltf_engine.py: BOS → диапазон → BSL-якорь → касание → отмена),
прогоняет LtfEngine. Использование:
    python tools/seed_ltf_demo.py C:/tmp/ltf_ui_test.db
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Instrument, Zone, ZoneStatus, ZoneType
from tests.conftest import H1_MS, make_h1_candles

# серия H из tests/test_ltf_engine.py (без импорта тестового модуля:
# значения продублированы, чтобы демо-засев не зависел от тестов)
SERIES_H_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 11),
    (14.0, 12.9), (12.8, 11.2), (11.0, 10.0), (10.2, 9.2), (8.8, 7.8),
    (8.2, 7.9), (8.4, 8.0), (8.3, 7.95), (9.5, 8.1),
    (8.6, 7.7), (8.4, 7.6), (8.2, 7.7), (8.1, 7.75), (8.2, 7.7),
    (9.8, 7.8),
]
SERIES_H_CLOSES = {10: 13.2, 11: 11.5, 12: 10.5, 13: 9.4, 14: 7.9, 20: 7.5,
                   24: 9.7}
T0 = 1_780_000_000_000


def main(path: str) -> None:
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass
    db = Database(path)
    iid = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    bars = []
    for i, (h, l) in enumerate(SERIES_H_HL):
        c = SERIES_H_CLOSES.get(i, (h + l) / 2)
        bars.append(((h + l) / 2, h, l, c))
    candles = make_h1_candles(bars, T0, iid)
    db.insert_candles(candles)
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=9.0, upper=10.0, formed_at=T0 - 10 * 86_400_000,
        confirmed_at=T0 - 9 * 86_400_000, status=ZoneStatus.ACTIVE,
    ))
    engine = LtfEngine(db, DetectorConfig())
    obs = engine.on_htf_zone_touched(iid, db.get_zone(zid), candles[0].open_time)
    res = engine.process_h1_close(iid, now_ms=candles[-1].close_time)
    print("события:", [(e.kind, (e.occurred_at - T0) // H1_MS) for e in res.events])
    print("сценарии:", res.scenarios_created, "диапазоны:", res.ranges_created,
          "pivots:", res.pivots_new, "наблюдение:", obs.id)
    db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "C:/tmp/ltf_ui_test.db")
