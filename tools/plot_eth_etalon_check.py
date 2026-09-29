"""Снимок ETH-текущего сетапа (этап 6, §18): H1 2026-09-19..24 из scratch-копии
data/diag/stage6/eth_replay.db (чистый replay текущего движка + догруженные H1),
поверх — эталонный FVG D1 2714.64-2754.53, BOS 2734.96 (23.09 09:59),
pivot-опоры 2789.0 (LH по эталону, роль HH в движке) и LL 2635.39.
Выход: data/diag/eth_etalon_check.png
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "diag" / "stage6" / "eth_replay.db"
OUT = ROOT / "data" / "diag" / "eth_etalon_check.png"


def ts(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000)


def main() -> None:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    t0 = int(datetime.datetime(2026, 9, 19).timestamp() * 1000)
    rows = c.execute(
        "select open_time, open, high, low, close from candle "
        "where instrument_id=2 and timeframe='H1' and open_time>=? order by open_time",
        (t0,)).fetchall()
    c.close()
    fig, ax = plt.subplots(figsize=(16, 8), dpi=110)
    w = 0.028
    for o, op, hi, lo, cl in rows:
        x = datetime.datetime.utcfromtimestamp(o / 1000)
        color = "#26a69a" if cl >= op else "#ef5350"
        ax.plot([x, x], [lo, hi], color=color, lw=0.7, zorder=2)
        ax.add_patch(Rectangle(
            (x - datetime.timedelta(hours=w * 24), min(op, cl)),
            datetime.timedelta(hours=w * 48), abs(cl - op) or 0.4,
            facecolor=color, edgecolor=color, zorder=3))

    x0 = ts(rows[0][0]) - datetime.timedelta(hours=6)
    x1 = ts(rows[-1][0]) + datetime.timedelta(hours=30)
    # FVG D1 (эталон): 2714.64–2754.53
    ax.add_patch(Rectangle((x0, 2714.64), x1 - x0, 2754.53 - 2714.64,
                           facecolor="#ef5350", alpha=0.18, zorder=1))
    for y in (2714.64, 2754.53):
        ax.axhline(y, color="#b71c1c", lw=1.0, zorder=2)
    ax.text(x1, 2745, "FVG D1 (медвежий) 2714.64–2754.53\nстатус движка: archived (артефакт replay,\nW1-свеча недели формирования)",
            color="#b71c1c", fontsize=8, va="center", ha="right")
    # BOS 2734.96 от pivot 22.09 22:00 до закрытия 23.09 09:59
    b0 = datetime.datetime(2026, 9, 22, 22)
    b1 = datetime.datetime(2026, 9, 23, 10)
    ax.plot([b0, b1], [2734.96, 2734.96], color="#1e88e5", lw=1.6, zorder=4)
    ax.text(b1, 2737, "BOS 2734.96 · закрытие H1 23.09 09:59 (эталон ~2730)",
            color="#1e88e5", fontsize=8, va="bottom")
    # опоры эталона: 2789.0 (23.09 04:00) и 2635.39 (23.09 16:00)
    ax.plot([datetime.datetime(2026, 9, 23, 4)], [2789.0], marker="v",
            color="#6a1b9a", markersize=8, zorder=5)
    ax.text(datetime.datetime(2026, 9, 23, 4), 2796, "опора эталона ~2793 (pivot 2789.0,\nроль HH → диапазон не построен)",
            color="#6a1b9a", fontsize=8, ha="center", va="bottom")
    ax.plot([datetime.datetime(2026, 9, 23, 16)], [2635.39], marker="^",
            color="#6a1b9a", markersize=8, zorder=5)
    ax.text(datetime.datetime(2026, 9, 23, 16), 2622, "LL 2635.39 (эталон ~2636),\nподтв. 19:59",
            color="#6a1b9a", fontsize=8, ha="center", va="top")
    ax.set_xlim(x0, x1)
    ax.set_ylim(2540, 2850)
    ax.set_title("ETHUSDT Binance H1 · 19–23.09.2026 · проверка эталона владельца (этап 6, п.23)\n"
                 "scratch-копия БД: чистый replay HTF + H1, догруженные из Binance (живая БД не менялась)")
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT)
    print("saved", OUT)


if __name__ == "__main__":
    sys.exit(main())
