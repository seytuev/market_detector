"""In-memory реестр метрик процесса (этап 1 perf-спеки — только наблюдение).

Потокобезопасен: запись идёт и из asyncio-цикла, и из рабочих потоков
(asyncio.to_thread движка). Никаких записей в SQLite — снимок живёт только
в памяти и отдаётся через GET /api/diagnostics/metrics.

Метки ограничены: набор ключей объявляется при регистрации серии
(register), неизвестные ключи отклоняются (ValueError) — job_id/symbol/
timestamp в метки не протащить. None приводится к "none".

Значения гистограмм пишутся в секундах, в snapshot отдаются в ms.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

_KINDS = ("counter", "histogram", "gauge")


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Ближайший ранг: p50 из 1..100 — 50-е значение."""
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


class _Counter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0.0


class _Gauge:
    __slots__ = ("value", "at")

    def __init__(self) -> None:
        self.value = 0.0
        self.at = 0.0


class _Histogram:
    __slots__ = ("window", "count", "sum")

    def __init__(self, window: int) -> None:
        self.window: deque[float] = deque(maxlen=window)
        self.count = 0
        self.sum = 0.0


class MetricsRegistry:
    """Счётчики/гистограммы/гауджи с ограниченным набором меток.

    window — размер скользящего окна наблюдений гистограммы (перцентили
    считаются по окну; count/sum — за всё время)."""

    def __init__(self, window: int = 2048, max_series: int = 1024) -> None:
        self._window = window
        self._max_series = max(1, max_series)
        self._lock = threading.Lock()
        self._series: dict[str, dict[str, Any]] = {}
        self._started = time.monotonic()

    def register(self, name: str, kind: str, label_keys: tuple[str, ...] = ()) -> None:
        """Объявить серию и допустимые ключи меток. Повторная регистрация
        с той же сигнатурой идемпотентна, с другой — ValueError."""
        if kind not in _KINDS:
            raise ValueError(f"unknown series kind: {kind!r}")
        with self._lock:
            self._register_locked(name, kind, tuple(label_keys))

    def _register_locked(self, name: str, kind: str, label_keys: tuple[str, ...]) -> dict:
        meta = self._series.get(name)
        if meta is not None:
            if meta["kind"] != kind or meta["label_keys"] != label_keys:
                raise ValueError(
                    f"series {name!r} already registered as {meta['kind']}{meta['label_keys']}"
                )
            return meta
        meta = {"kind": kind, "label_keys": label_keys, "series": {}}
        self._series[name] = meta
        return meta

    def _holder(self, name: str, kind: str, labels: dict[str, Any]):
        with self._lock:
            meta = self._series.get(name)
            if meta is None:
                meta = self._register_locked(name, kind, tuple(labels.keys()))
            elif meta["kind"] != kind:
                raise ValueError(f"series {name!r} registered as {meta['kind']}, not {kind}")
            unknown = set(labels) - set(meta["label_keys"])
            if unknown:
                raise ValueError(f"series {name!r}: undeclared label keys {sorted(unknown)}")
            key = tuple(
                "none" if labels.get(k) is None else str(labels[k])
                for k in meta["label_keys"]
            )
            holder = meta["series"].get(key)
            if holder is None:
                if sum(len(series["series"]) for series in self._series.values()) >= self._max_series:
                    key = tuple("overflow" for _ in meta["label_keys"])
                    holder = meta["series"].get(key)
                    if holder is not None:
                        return holder
                if kind == "counter":
                    holder = _Counter()
                elif kind == "gauge":
                    holder = _Gauge()
                else:
                    holder = _Histogram(self._window)
                meta["series"][key] = holder
            return holder

    @staticmethod
    def _merge(labels: Optional[dict[str, Any]], kw: dict[str, Any]) -> dict[str, Any]:
        if labels and kw:
            return {**labels, **kw}
        return labels or kw

    def counter(self, name: str, value: float = 1.0,
                labels: Optional[dict[str, Any]] = None, **kw: Any) -> None:
        """Монотонный счётчик: counter('quote_ticks', venue=..., source=...)."""
        holder = self._holder(name, "counter", self._merge(labels, kw))
        with self._lock:
            holder.value += value

    def gauge(self, name: str, value: float,
              labels: Optional[dict[str, Any]] = None, **kw: Any) -> None:
        """Последнее значение."""
        holder = self._holder(name, "gauge", self._merge(labels, kw))
        with self._lock:
            holder.value = float(value)
            holder.at = time.time()

    def histogram(self, name: str, value_seconds: float,
                  labels: Optional[dict[str, Any]] = None, **kw: Any) -> None:
        """Наблюдение длительности, секунды (в snapshot — ms)."""
        holder = self._holder(name, "histogram", self._merge(labels, kw))
        with self._lock:
            holder.window.append(float(value_seconds))
            holder.count += 1
            holder.sum += float(value_seconds)

    @contextmanager
    def timer(self, name: str, labels: Optional[dict[str, Any]] = None,
              **kw: Any) -> Iterator[None]:
        """with registry.timer('rest_request', venue='binance'): ..."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.histogram(name, time.monotonic() - started, labels, **kw)

    @staticmethod
    def _series_key(name: str, label_keys: tuple[str, ...], key: tuple[str, ...]) -> str:
        if not label_keys:
            return name
        pairs = ",".join(f"{k}={v}" for k, v in zip(label_keys, key))
        return f"{name}{{{pairs}}}"

    def snapshot(self) -> dict[str, Any]:
        """JSON-сериализуемый снимок: uptime_s, collected_at, series{...}."""
        with self._lock:
            uptime = time.monotonic() - self._started
            copied = {
                name: (meta["kind"], meta["label_keys"],
                       {key: holder for key, holder in meta["series"].items()})
                for name, meta in self._series.items()
            }
            windows = {
                id(holder): sorted(holder.window)
                for _, _, series in copied.values()
                for holder in series.values()
                if isinstance(holder, _Histogram)
            }
        out: dict[str, Any] = {}
        for name, (kind, label_keys, series) in copied.items():
            for key, holder in series.items():
                flat = self._series_key(name, label_keys, key)
                if isinstance(holder, _Counter):
                    out[flat] = {"kind": kind, "value": holder.value}
                elif isinstance(holder, _Gauge):
                    out[flat] = {"kind": kind, "value": holder.value, "at": holder.at}
                else:
                    values = windows[id(holder)]
                    if values:
                        out[flat] = {
                            "kind": kind,
                            "count": holder.count,
                            "sum": round(holder.sum * 1000, 3),
                            "p50_ms": round(_percentile(values, 50) * 1000, 3),
                            "p95_ms": round(_percentile(values, 95) * 1000, 3),
                            "p99_ms": round(_percentile(values, 99) * 1000, 3),
                            "max_ms": round(values[-1] * 1000, 3),
                        }
                    else:
                        out[flat] = {"kind": kind, "count": 0, "sum": 0.0,
                                     "p50_ms": None, "p95_ms": None,
                                     "p99_ms": None, "max_ms": None}
        return {
            "uptime_s": round(uptime, 3),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "series": out,
        }


_REGISTRY = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    """Процессный синглтон; тесты могут строить независимые экземпляры."""
    return _REGISTRY
