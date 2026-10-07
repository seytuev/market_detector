"""Синтетические D1-серии для эталонов alt range engine v2 (этап 1).

Используются, когда реальные OHLC Binance недоступны (сеть/геоблок):
tools/fetch_alt_etalons.py завершается ошибкой — тогда этот модуль
генерирует серии, воспроизводящие СТРУКТУРУ каждого примера из
tests/fixtures/alt_v2/markup.json: ATH → глубокое падение (≥80%) →
первая база → импульсный выход → вторая база → выносы ниже L →
выходы/ретесты. Даты и уровни согласованы с markup.json (допустимые
интервалы L/U), сид детерминирован — серии воспроизводимы.

Формат свечи — как у fetch_alt_etalons.py:
{open_time, open, high, low, close, volume}, open_time в ms UTC.

Запись в tests/fixtures/alt_v2/ohlc/ выполняется ТОЛЬКО для тикеров,
у которых нет реального файла (реальные данные не перезаписываются).

Запуск: .venv/Scripts/python.exe tests/fixtures/alt_v2/synthetic.py
"""
from __future__ import annotations

import datetime
import json
import math
import random
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
OHLC_DIR = DIR / "ohlc"

DAY_MS = 86_400_000
AS_OF = "2026-10-06"
SEED = 20261006


def d(date: str) -> int:
    dt = datetime.datetime.strptime(date, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp() * 1000)


class _Builder:
    """Построитель дневной серии из последовательных сегментов."""

    def __init__(self, ticker: str, start: str, end: str = AS_OF):
        self.rng = random.Random(f"{SEED}:{ticker}")
        self.start_ms = d(start)
        self.end_ms = d(end)
        self.closes: dict[int, float] = {}
        self.last_price: float | None = None

    def _fill(self, start: str, end: str, price_fn) -> None:
        t0, t1 = d(start), d(end)
        days = max(1, (t1 - t0) // DAY_MS)
        p = self.last_price if self.last_price is not None else price_fn(0.0)
        for i in range(days + 1):
            target = price_fn(i / days)
            # плавный дрейф к цели сегмента + шум
            p = p + (target - p) * 0.35
            noise = 1.0 + self.rng.uniform(-0.02, 0.02)
            self.closes[t0 + i * DAY_MS] = max(p * noise, 1e-9)
        self.last_price = self.closes[t1]

    def trend(self, start: str, end: str, p_from: float, p_to: float) -> None:
        """Направленное движение (рост к ATH, импульсный выход, падение)."""
        self._fill(start, end, lambda f: p_from + (p_to - p_from) * f)

    def base(self, start: str, end: str, low: float, high: float) -> None:
        """Боковик накопления: колебания между L и U."""
        mid = (low + high) / 2
        amp = (high - low) / 2
        period = self.rng.uniform(24, 40)
        phase = self.rng.uniform(0, 2 * math.pi)

        def price(f: float) -> float:
            days = f * ((d(end) - d(start)) // DAY_MS)
            wobble = 1.0 + self.rng.uniform(-0.05, 0.05)
            return mid + amp * wobble * math.sin(phase + 2 * math.pi * days / period)

        self._fill(start, end, price)

    def sweep(self, start: str, end: str, floor: float, back: float) -> None:
        """Вынос ниже L: провал к floor в середине участка и возврат к back."""
        def price(f: float) -> float:
            if f < 0.35:
                return back + (floor - back) * (f / 0.35)
            if f < 0.65:
                return floor * 1.03
            return floor + (back - floor) * ((f - 0.65) / 0.35)

        self._fill(start, end, price)

    def build(self) -> list[dict]:
        rows = []
        prev_close = None
        for t in range(self.start_ms, self.end_ms + DAY_MS, DAY_MS):
            close = self.closes.get(t)
            if close is None:
                # разрыв между сегментами — держим последнюю цену
                close = prev_close if prev_close is not None else 1.0
            open_ = prev_close if prev_close is not None else close
            hi = max(open_, close) * (1.0 + self.rng.uniform(0.002, 0.02))
            lo = min(open_, close) * (1.0 - self.rng.uniform(0.002, 0.02))
            rows.append({
                "open_time": t,
                "open": round(open_, 10),
                "high": round(hi, 10),
                "low": round(max(lo, 1e-9), 10),
                "close": round(close, 10),
                "volume": round(self.rng.uniform(1e5, 1e7), 2),
            })
            prev_close = close
        return rows


def gen_near() -> list[dict]:
    """ATH 20.6 (01.2022) → −95% → база 1.0–2.9 (2022–23) → выход →
    спад → новая база 1.6–3.6 (2025–26) → вынос ~0.85 → возврат →
    выход вверх 09.2026."""
    b = _Builder("NEAR", "2020-10-14")
    b.trend("2020-10-14", "2022-01-14", 1.0, 20.6)
    b.trend("2022-01-15", "2022-06-01", 20.0, 1.1)
    b.base("2022-06-01", "2023-10-31", 1.0, 2.9)
    b.trend("2023-11-01", "2024-03-15", 3.0, 9.0)
    b.trend("2024-03-16", "2025-05-31", 8.5, 2.0)
    b.base("2025-06-01", "2025-11-30", 1.6, 3.6)
    b.sweep("2025-12-01", "2026-04-30", 0.85, 1.7)
    b.base("2026-05-01", "2026-09-09", 1.6, 3.6)
    b.trend("2026-09-10", "2026-10-06", 3.4, 5.2)
    return b.build()


def gen_sui() -> list[dict]:
    """ATH 5.37 (01.2025) → снижение начала 2026 → локальная база
    0.7–1.35 → летний вынос ~0.635 → возврат в тело базы."""
    b = _Builder("SUI", "2023-05-03")
    b.trend("2023-05-03", "2025-01-06", 0.4, 5.37)
    b.trend("2025-01-07", "2026-02-01", 5.0, 0.85)
    b.base("2026-02-01", "2026-05-31", 0.7, 1.35)
    b.sweep("2026-06-01", "2026-08-31", 0.635, 0.8)
    b.base("2026-09-01", "2026-10-06", 0.7, 1.35)
    return b.build()


def gen_hbar() -> list[dict]:
    """ATH 0.576 (09.2021) → старая база 0.04–0.135 → импульс конца
    2024 → спад → новая база 0.075–0.11 (2026) → летний вынос ~0.064."""
    b = _Builder("HBAR", "2019-09-29")
    b.trend("2019-09-29", "2021-09-16", 0.01, 0.576)
    b.trend("2021-09-17", "2022-06-01", 0.5, 0.05)
    b.base("2022-06-01", "2024-10-31", 0.04, 0.135)
    b.trend("2024-11-01", "2024-12-15", 0.14, 0.40)
    b.trend("2024-12-16", "2025-12-31", 0.38, 0.085)
    b.base("2026-01-01", "2026-05-31", 0.075, 0.11)
    b.sweep("2026-06-01", "2026-08-31", 0.064, 0.08)
    b.base("2026-09-01", "2026-10-06", 0.075, 0.11)
    return b.build()


def gen_tao() -> list[dict]:
    """ATH 1249 (04.2024) → −88% → ШИРОКАЯ база 140–500 (контроль:
    рамку нельзя чрезмерно сужать)."""
    b = _Builder("TAO", "2024-04-11")
    b.trend("2024-04-11", "2024-04-20", 1200.0, 1249.0)
    b.trend("2024-04-21", "2025-05-31", 1100.0, 300.0)
    b.base("2025-06-01", "2026-10-06", 140.0, 500.0)
    return b.build()


def gen_ena() -> list[dict]:
    """ATH 1.52 (04.2024) → спад → нижняя база 0.08–0.14 (2026) →
    летние минимумы ~0.070 отдельно → выход 08–09.2026 → ретест ~0.14."""
    b = _Builder("ENA", "2024-04-02")
    b.trend("2024-04-02", "2024-04-11", 1.3, 1.523)
    b.trend("2024-04-12", "2026-01-14", 1.4, 0.12)
    b.base("2026-01-15", "2026-05-31", 0.08, 0.14)
    b.sweep("2026-06-01", "2026-07-31", 0.070, 0.085)
    b.base("2026-08-01", "2026-08-10", 0.08, 0.14)
    b.trend("2026-08-11", "2026-09-05", 0.15, 0.29)
    b.trend("2026-09-06", "2026-09-20", 0.28, 0.14)
    b.trend("2026-09-21", "2026-10-06", 0.15, 0.24)
    return b.build()


def gen_pump() -> list[dict]:
    """Листинг 0.009 (09.2025) → −95% → база 0.0017–0.0034 → летний
    выход вниз ~0.00115 отдельно → выход вверх 08.2026 → ретест ~0.0034."""
    b = _Builder("PUMP", "2025-09-11")
    b.trend("2025-09-11", "2025-09-14", 0.008, 0.009)
    b.trend("2025-09-15", "2025-11-30", 0.0085, 0.0018)
    b.base("2025-12-01", "2026-05-31", 0.0017, 0.0034)
    b.sweep("2026-06-01", "2026-07-31", 0.00115, 0.0018)
    b.base("2026-08-01", "2026-08-05", 0.0017, 0.0034)
    b.trend("2026-08-06", "2026-08-31", 0.0035, 0.0054)
    b.trend("2026-09-01", "2026-09-20", 0.0052, 0.0034)
    b.trend("2026-09-21", "2026-10-06", 0.0036, 0.0062)
    return b.build()


def gen_aave() -> list[dict]:
    """ATH 668 (05.2021) → −92% → историческая база 50–125 (2022–24) →
    выход конца 2024 → ретесты зоны ~125; база НЕ актуальна в 10.2026."""
    b = _Builder("AAVE", "2020-10-15")
    b.trend("2020-10-15", "2021-05-18", 30.0, 668.0)
    b.trend("2021-05-19", "2022-06-01", 600.0, 55.0)
    b.base("2022-06-01", "2024-10-31", 50.0, 125.0)
    b.trend("2024-11-01", "2024-12-15", 130.0, 380.0)
    b.trend("2024-12-16", "2025-12-31", 350.0, 150.0)
    b.trend("2026-01-01", "2026-01-31", 160.0, 125.0)  # ретест зоны бывшего U
    b.trend("2026-02-01", "2026-06-30", 120.0, 60.0)
    b.trend("2026-07-01", "2026-10-06", 65.0, 180.0)
    return b.build()


def gen_ondo() -> list[dict]:
    """ATH 1.17 (07.2025) → −83% → нижняя база 0.20–0.295 (нач. 2026) →
    майский выход → июньско-июльский возврат (сопровождение) →
    поздний диапазон 0.33–0.49 — отдельный кандидат."""
    b = _Builder("ONDO", "2025-04-11")
    b.trend("2025-04-11", "2025-07-22", 0.6, 1.1695)
    b.trend("2025-07-23", "2026-01-14", 1.1, 0.22)
    b.base("2026-01-15", "2026-04-30", 0.20, 0.295)
    b.trend("2026-05-01", "2026-05-31", 0.30, 0.49)
    b.trend("2026-06-01", "2026-06-30", 0.47, 0.30)  # возврат к бывшему U
    b.trend("2026-07-01", "2026-07-31", 0.31, 0.43)
    b.base("2026-08-01", "2026-10-06", 0.33, 0.49)
    return b.build()


GENERATORS = {
    "NEAR": gen_near,
    "SUI": gen_sui,
    "HBAR": gen_hbar,
    "TAO": gen_tao,
    "ENA": gen_ena,
    "PUMP": gen_pump,
    "AAVE": gen_aave,
    "ONDO": gen_ondo,
}


def generate(ticker: str) -> list[dict]:
    """Детерминированная синтетическая D1-серия по тикеру из markup.json."""
    return GENERATORS[ticker.upper()]()


def write_missing() -> list[str]:
    """Записать синтетику только для тикеров без реального OHLC-файла.

    Возвращает список тикеров, для которых записана синтетика.
    Реальные файлы Binance не перезаписываются.
    """
    OHLC_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for ticker in GENERATORS:
        path = OHLC_DIR / f"{ticker}.json"
        if path.exists():
            continue
        rows = generate(ticker)
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        written.append(ticker)
    return written


if __name__ == "__main__":
    done = write_missing()
    if done:
        print(f"записана СИНТЕТИКА (реальные OHLC отсутствовали): {', '.join(done)}")
        print("отметьте источник данных в tests/fixtures/alt_v2/README.md")
    else:
        print("реальные OHLC на месте для всех тикеров; синтетика не записана")
    sys.exit(0)
