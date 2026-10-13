"""Базовый бенчмарк конвейера (этап 1 perf-спеки): детерминированные
сценарии без сети и Telegram, замеры — через app.services.metrics
(глобальный реестр, т.е. в отчёт попадают и sql_duration инструментированного
слоя БД). БД — временные файлы (не :memory:), удаляются после прогона.

Сценарии:
  quote_storm   1000 синтетических тиков (~5% дублей, ~2% вне порядка) через
                текущий quote-путь (этап 2): DataSync.request (stub-адаптер) →
                быстрый ingest (дедуп → db.set_quote_if_fresh → формирующийся
                бар → enqueue в market_queue), затем дочитка очереди
                (calculate(scanner.on_price) на каждый принятый тик).
                bench_quote_tick мерит быстрый путь; drain_s — дочитку.
  startup       время Database(path): open+schema+migrations на файле с
                N H1-свечей на инструмент (генерация — executemany в
                batch_writes, без логики ревизий).
  candle_close  закрытие 5 H1-свечей на инструмент: локальная часть
                sync_candles (get_candles(limit=200) + единица batch_writes
                с insert_candles и курсором meta), сеть не участвует.

CLI: --scenario all|quote_storm|startup|candle_close --instruments 3
     --ticks 1000 --candles 30000 --out data/diag/baseline_<ts>.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.base import QuoteTick  # noqa: E402
from app.config import DetectorConfig, Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import Candle, Instrument, now_ms  # noqa: E402
from app.notify.queue import EventDispatcher  # noqa: E402
from app.notify.telegram import LogSender  # noqa: E402
from app.services.data_sync import period_open  # noqa: E402
from app.services.market_queue import MarketQueue  # noqa: E402
from app.services.metrics import get_registry  # noqa: E402
from app.worker import Worker  # noqa: E402

H1_MS = 3_600_000
CANDLE_SQL = (
    "INSERT INTO candle (instrument_id, timeframe, open_time, close_time,"
    " open, high, low, close, closed, source) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def make_candle(open_time: int, o: float, h: float, l: float, c: float,
                timeframe: str, instrument_id: int, closed: bool = True,
                source: str = "test") -> Candle:
    tf_ms = {"H1": 60, "H4": 240, "D1": 1440, "W1": 10080}[timeframe] * 60_000
    return Candle(instrument_id=instrument_id, timeframe=timeframe,
                  open_time=open_time, close_time=open_time + tf_ms - 1,
                  open=o, high=h, low=l, close=c, closed=closed, source=source)


def gen_h1_rows(instrument_id: int, count: int, end_open: int, seed: int) -> list[tuple]:
    """Детерминированный random-walk; только закрытые свечи, шаг 1ч."""
    rng = random.Random(seed)
    price = 100.0
    rows = []
    first_open = end_open - (count - 1) * H1_MS
    for i in range(count):
        o = price
        c = o + rng.uniform(-1, 1)
        h = max(o, c) + rng.uniform(0, 0.5)
        l = min(o, c) - rng.uniform(0, 0.5)
        ot = first_open + i * H1_MS
        rows.append((instrument_id, "H1", ot, ot + H1_MS - 1,
                     round(o, 4), round(h, 4), round(l, 4), round(c, 4), 1, "test"))
        price = c
    return rows


def add_instruments(db: Database, n: int) -> list[int]:
    ids = []
    for i in range(n):
        ids.append(db.upsert_instrument(Instrument(
            id=None, asset=f"ASSET{i}", venue="test", market_type="spot",
            symbol=f"ASSET{i}USDT", quote_asset="USDT",
        )))
    return ids


def build_candle_db(path: str, instruments: int, candles_per: int) -> dict:
    """Файл с candles_per закрытых H1-свечей на инструмент (быстро:
    executemany в одном batch_writes, без per-write commit)."""
    t0 = time.perf_counter()
    db = Database(path)
    ids = add_instruments(db, instruments)
    end_open = period_open("H1", now_ms()) - H1_MS
    with db.batch_writes():
        for iid in ids:
            db.conn.executemany(CANDLE_SQL, gen_h1_rows(iid, candles_per, end_open, iid))
    db.close()
    return {"fill_s": round(time.perf_counter() - t0, 3),
            "candles": candles_per * instruments, "instrument_ids": ids}


class StubAdapter:
    """last_price отдаёт заранее сгенерированные тики; сети нет."""

    venue = "test"

    def __init__(self, ticks: list[QuoteTick]):
        self._ticks = ticks
        self._i = 0

    async def last_price(self, symbol: str) -> QuoteTick:
        tick = self._ticks[self._i]
        self._i += 1
        return tick

    async def klines(self, symbol, timeframe, start_ms, end_ms, include_forming=False):
        return []

    async def catalog(self):
        return []

    async def status(self):
        return {}


def gen_ticks(n: int, window_ms: int = 60_000, seed: int = 42) -> list[QuoteTick]:
    """n тиков на окне window_ms, ~5% дублей (event_at предыдущего),
    ~2% вне порядка (старше уже виденного максимума)."""
    rng = random.Random(seed)
    base = now_ms() - window_ms
    event_times = [base + int(i * window_ms / n) for i in range(n)]
    for i in range(1, n):
        if rng.random() < 0.05:
            event_times[i] = event_times[i - 1]
    for i in range(2, n):
        if rng.random() < 0.02:
            event_times[i] = event_times[i - 2] - rng.randint(1, 50)
    price = 100.0
    ticks = []
    for et in event_times:
        price += rng.uniform(-0.05, 0.05)
        ticks.append(QuoteTick(price=round(price, 4), event_at=et,
                               received_at=et + rng.randint(5, 40),
                               price_kind="trade"))
    return ticks


async def _quote_storm_async(db: Database, ins, ticks: list[QuoteTick]) -> dict:
    cfg = DetectorConfig()
    settings = Settings()
    worker = Worker(db, settings, cfg, {"test": StubAdapter(ticks)},
                    EventDispatcher(db, cfg, LogSender()), ltf_engine=None)
    # ёмкость ≥ числа тиков: stub-адаптер не даёт точек выхода в цикл,
    # потребитель за цикл опроса не успевает забрать элементы — без этого
    # шторм упирался бы в overflow-спул вместо чистого быстрого пути
    worker.market_queue = MarketQueue(worker, queue_capacity=max(500, len(ticks)))
    # формирующиеся бары, чтобы путь _apply_price_to_forming работал
    now = now_ms()
    for tf in dict.fromkeys((*worker.scan_tfs, "H1")):
        db.insert_candles([make_candle(period_open(tf, now), 100, 101, 99, 100,
                                       tf, ins.id, closed=False)])
    metrics = get_registry()
    accepted = 0
    prev_ts = None
    t0 = time.perf_counter()
    for _ in ticks:
        t_tick = time.perf_counter()
        await worker._poll_quote(ins)
        metrics.histogram("bench_quote_tick", time.perf_counter() - t_tick)
        # тик «принят», если сдвинул сохранённую котировку (дубли с тем же
        # event_at пересчитываются, но котировку не двигают; внепорядковые
        # отбрасываются фильтром монотонности целиком)
        detail = db.get_quote_detail(ins.id)
        ts = detail["ts"] if detail else None
        if ts != prev_ts:
            accepted += 1
        prev_ts = ts
    fast_s = time.perf_counter() - t0
    # тяжёлая обработка (scanner.on_price) с этапа 2 — в потребителе очереди;
    # дочитываем, чтобы wall оставался сопоставимым «конец-в-конец»
    t_drain = time.perf_counter()
    drained = await worker.market_queue.wait_idle(ins.id, 600.0)
    drain_s = time.perf_counter() - t_drain
    await worker.market_queue.stop()
    detail = db.get_quote_detail(ins.id)
    return {"wall_s": round(fast_s + drain_s, 3),
            "fast_path_s": round(fast_s, 3), "drain_s": round(drain_s, 3),
            "drained": drained, "ticks": len(ticks),
            "accepted": accepted, "not_advanced": len(ticks) - accepted,
            "final_price": detail["price"] if detail else None}


def scenario_quote_storm(tmp: str, args) -> dict:
    path = str(Path(tmp) / "quote_storm.db")
    db = Database(path)
    iid = add_instruments(db, 1)[0]
    ins = db.get_instrument(iid)
    ticks = gen_ticks(args.ticks)
    result = asyncio.run(_quote_storm_async(db, ins, ticks))
    db.close()
    result["scope"] = ("этап 2: Worker._poll_quote — быстрый путь "
                       "(DataSync.request stub → дедуп → set_quote_if_fresh → "
                       "формирующийся бар → enqueue); bench_quote_tick мерит "
                       "только быстрый путь, drain_s — дочитка market_queue "
                       "(calculate(on_price)) в конце; без сети/Telegram/WS")
    result["db_bytes"] = os.path.getsize(path)
    return result


def scenario_startup(tmp: str, args) -> dict:
    path = str(Path(tmp) / "startup.db")
    built = build_candle_db(path, args.instruments, args.candles)
    db_bytes = os.path.getsize(path)
    metrics = get_registry()
    t0 = time.perf_counter()
    db = Database(path)  # open + schema check + идемпотентные миграции
    db.close()
    wall = time.perf_counter() - t0
    metrics.histogram("bench_startup_open", wall)
    return {"scope": "Database(path): sqlite connect + schema/migrations на "
                     "предзаполненном файле",
            "wall_s": round(wall, 3), "db_bytes": db_bytes, **built}


def scenario_candle_close(tmp: str, args) -> dict:
    path = str(Path(tmp) / "candle_close.db")
    built = build_candle_db(path, args.instruments, 500)
    db = Database(path)
    metrics = get_registry()
    batches = 0
    t_all = time.perf_counter()
    for iid in built["instrument_ids"]:
        t0 = time.perf_counter()
        # локальная часть sync_candles после получения свечей от адаптера
        # (этап 7: окно хвоста limit=200 + одна единица записи §12)
        rows = db.get_candles(iid, "H1", limit=200)
        before = {c.open_time: c for c in rows}
        tail_open = rows[-1].open_time
        incoming = []
        price = rows[-1].close
        for i in range(1, 6):
            ot = tail_open + i * H1_MS
            c = price + 0.3
            incoming.append(make_candle(ot, price, c + 0.4, price - 0.4, c, "H1", iid))
            price = c
        assert all(c.open_time not in before for c in incoming)
        with db.batch_writes():
            db.insert_candles(incoming)
            db.set_meta(f"data_sync:processed:{iid}:H1", str(incoming[-1].close_time))
        metrics.histogram("bench_candle_close", time.perf_counter() - t0)
        batches += 1
    wall = time.perf_counter() - t_all
    db_bytes = os.path.getsize(path)
    db.close()
    return {"scope": "локальная часть sync_candles (этап 7): get_candles(limit=200) + "
                     "batch_writes(insert_candles(5 закрытых H1) + meta-курсор); "
                     "сеть не участвует",
            "wall_s": round(wall, 3), "batches": batches, "db_bytes": db_bytes}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario",
                    choices=["all", "quote_storm", "startup", "candle_close"],
                    default="all")
    ap.add_argument("--instruments", type=int, default=3)
    ap.add_argument("--ticks", type=int, default=1000)
    ap.add_argument("--candles", type=int, default=30_000)
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="baseline_bench_")
    print(f"временные БД: {tmp}")
    scenarios: dict[str, dict] = {}
    try:
        selected = (["quote_storm", "startup", "candle_close"]
                    if args.scenario == "all" else [args.scenario])
        for name in selected:
            print(f"--- {name}", flush=True)
            fn = {"quote_storm": scenario_quote_storm,
                  "startup": scenario_startup,
                  "candle_close": scenario_candle_close}[name]
            scenarios[name] = fn(tmp, args)
            print(f"    wall={scenarios[name]['wall_s']} c", flush=True)
        report = {
            "params": {"instruments": args.instruments, "ticks": args.ticks,
                       "candles_per_instrument": args.candles},
            "scenarios": scenarios,
            "metrics": get_registry().snapshot(),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "db_bytes": max((s.get("db_bytes", 0) for s in scenarios.values()),
                                default=0),
                "note": "local measurement",
            },
        }
        out = Path(args.out) if args.out else (
            Path("data/diag") / f"baseline_{int(time.time())}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"отчёт: {out}")
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
