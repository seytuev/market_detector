"""Реестр метрик (этап 1 perf-спеки): счётчики/гистограммы/гауджи,
ограниченные метки, скользящее окно, снимок, таймер, SQL-обвязка."""
from __future__ import annotations

import json

import pytest

from app.db import Database
from app.services.metrics import MetricsRegistry, get_registry


def series_of(reg: MetricsRegistry) -> dict:
    return reg.snapshot()["series"]


def test_counter_basic():
    reg = MetricsRegistry()
    reg.counter("hits")
    reg.counter("hits")
    reg.counter("hits", 3.5)
    reg.register("req", "counter", ("venue",))
    reg.counter("req", venue="binance")
    series = series_of(reg)
    assert series["hits"] == {"kind": "counter", "value": 5.5}
    assert series["req{venue=binance}"] == {"kind": "counter", "value": 1.0}


def test_histogram_percentiles_known_data():
    reg = MetricsRegistry()
    for i in range(1, 101):  # 1..100 ms
        reg.histogram("op", i / 1000)
    snap = series_of(reg)["op"]
    assert snap["count"] == 100
    assert snap["p50_ms"] == 50
    assert snap["p95_ms"] == 95
    assert snap["p99_ms"] == 99
    assert snap["max_ms"] == 100
    assert snap["sum"] == pytest.approx(5050, abs=0.001)


def test_histogram_empty_series():
    reg = MetricsRegistry()
    reg.register("op", "histogram", ("venue",))
    snap = series_of(reg)
    assert "op" not in snap  # без наблюдений инстанса не существует


def test_gauge_last_value_and_at():
    reg = MetricsRegistry()
    reg.gauge("temp", 1.5)
    reg.gauge("temp", 2.5)
    snap = series_of(reg)["temp"]
    assert snap["value"] == 2.5 and snap["at"] > 0


def test_undeclared_label_key_rejected():
    reg = MetricsRegistry()
    reg.register("req", "counter", ("venue",))
    reg.counter("req", venue="binance")
    with pytest.raises(ValueError):
        reg.counter("req", venue="binance", symbol="BTCUSDT")
    with pytest.raises(ValueError):
        reg.counter("req", job_id="abc")


def test_none_label_coerced_to_none_string():
    reg = MetricsRegistry()
    reg.counter("req", venue=None)
    assert "req{venue=none}" in series_of(reg)


def test_series_kind_conflict_rejected():
    reg = MetricsRegistry()
    reg.counter("x")
    with pytest.raises(ValueError):
        reg.histogram("x", 0.1)
    reg.register("y", "counter", ("a",))
    with pytest.raises(ValueError):
        reg.register("y", "counter", ("a", "b"))
    reg.register("y", "counter", ("a",))  # идемпотентно


def test_sliding_window_drops_old():
    reg = MetricsRegistry(window=5)
    for i in range(1, 11):  # 1..10 ms; окно хранит 6..10
        reg.histogram("op", i / 1000)
    snap = series_of(reg)["op"]
    assert snap["count"] == 10  # счётчик за всё время
    assert snap["max_ms"] == 10
    assert snap["p50_ms"] == 8  # ближайший ранг по окну [6,7,8,9,10]


def test_snapshot_is_json_serializable():
    reg = MetricsRegistry()
    reg.counter("c", venue="binance")
    reg.histogram("h", 0.005, route="/api/x")
    reg.gauge("g", 42)
    snap = reg.snapshot()
    assert snap["uptime_s"] >= 0
    assert "collected_at" in snap
    json.dumps(snap)  # не падает


def test_timer_context_manager_records():
    reg = MetricsRegistry()
    with reg.timer("block", task="on_price"):
        sum(range(1000))
    snap = series_of(reg)["block{task=on_price}"]
    assert snap["count"] == 1 and snap["sum"] >= 0
    with pytest.raises(RuntimeError):
        with reg.timer("block", task="on_price"):
            raise RuntimeError("boom")
    assert series_of(reg)["block{task=on_price}"]["count"] == 2


def test_independent_instances_are_isolated():
    a, b = MetricsRegistry(), MetricsRegistry()
    a.counter("x")
    assert "x" not in series_of(b)


def test_sql_instrumentation_smoke(db: Database):
    db.set_meta("k", "v")
    db.get_meta("k")
    db.conn.execute("SELECT 1")
    db.conn.commit()
    series = get_registry().snapshot()["series"]
    sql = {k: v for k, v in series.items() if k.startswith("sql_duration{")}
    assert sql, "sql_duration series missing"
    assert sum(v["count"] for v in sql.values()) > 0
    kinds = {k for k in sql}
    assert any("kind=read" in k for k in kinds)
    assert any("kind=write" in k for k in kinds)
    assert any("op=commit" in k for k in kinds)
