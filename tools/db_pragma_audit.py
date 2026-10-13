"""Аудит PRAGMA/конфигурации SQLite (этап 7 perf-спеки, §12).

Только чтение: база открывается `mode=ro` (URI), без журналируемых операций —
отдельный бэкап через SQLite backup API не нужен (конвенция записи с бэкапом —
app/services/taken_levels_migration.py:_backup). Отчёт:

  * PRAGMA живой БД: journal_mode, synchronous, wal_autocheckpoint,
    cache_size, busy_timeout (соединения аудита), page_size/page_count,
    freelist, размер WAL-файла;
  * политика busy_timeout по РОЛЯМ соединений из app/storage.py — измеряется
    живым зондом на временной БД (Connection(":memory:") → временный файл),
    а не копипастой констант: цикл событий → 0 (мгновенный SQLITE_BUSY,
    async-писатели обязаны идти через DataSync._write), потоки → 15000 мс;
  * findings: расхождения с ожиданиями perf-спеки (WAL, synchronous>=NORMAL)
    и известные проектные решения — без изменения конфигурации.

CLI: --db data/htf_zones.db --out data/diag/pragma_audit_<ts>.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PRAGMAS = (
    "journal_mode", "synchronous", "wal_autocheckpoint", "cache_size",
    "busy_timeout", "page_size", "page_count", "freelist_count",
)
SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}


def probe_role_timeouts() -> dict:
    """busy_timeout по ролям соединений app.storage.Connection (storage.py:
    PRAGMA busy_timeout=0 на соединении цикла событий, 15000 мс на потоковых).
    Зонд — временная БД, целевая не трогается."""
    from app.storage import Connection

    conn = Connection(":memory:")
    try:
        roles = {}
        # главный поток без running loop — роль «потоковое соединение»
        roles["thread_no_loop"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        async def _loop_probe() -> int:
            return conn.execute("PRAGMA busy_timeout").fetchone()[0]

        roles["event_loop"] = asyncio.run(_loop_probe())
        # отдельный рабочий поток (FIFO-писатель DataSync._write, расчёт)
        result: dict[str, int] = {}

        def _worker_probe():
            result["value"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        t = threading.Thread(target=_worker_probe, name="pragma-probe")
        t.start()
        t.join()
        roles["worker_thread"] = result["value"]
        return roles
    finally:
        conn.close()


def audit_db(db_path: Path) -> dict:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        pragmas = {}
        for name in PRAGMAS:
            pragmas[name] = con.execute(f"PRAGMA {name}").fetchone()[0]
        counts = {}
        for table in ("candle", "zone", "event", "quote_spool", "calc_task"):
            try:
                counts[table] = con.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = None  # таблицы нет (старая схема)
    finally:
        con.close()
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    return {
        "db_path": str(db_path),
        "db_bytes": db_path.stat().st_size if db_path.exists() else None,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
        "sqlite_version": sqlite3.sqlite_version,
        "pragmas": pragmas,
        "row_counts": counts,
    }


def findings(report: dict) -> list[dict]:
    out = []
    p = report["pragmas"]
    if p["journal_mode"].lower() != "wal":
        out.append({"severity": "problem", "finding":
                    f"journal_mode={p['journal_mode']} — ожидается WAL "
                    f"(app/storage.py:32 ставит при открытии; проверьте, "
                    f"что база открывалась приложением)"})
    if p["synchronous"] < 1:
        out.append({"severity": "problem", "finding":
                    f"synchronous={p['synchronous']} (OFF) — потеря данных "
                    f"при сбое питания; спека не разрешает снижать durability"})
    roles = report["connection_roles"]
    if roles.get("event_loop") != 0:
        out.append({"severity": "problem", "finding":
                    f"busy_timeout цикла событий = {roles.get('event_loop')} — "
                    f"контракт storage требует 0 (мгновенный BUSY вместо "
                    f"заморозки цикла); проверьте app/storage.py"})
    if roles.get("worker_thread") != 15000:
        out.append({"severity": "problem", "finding":
                    f"busy_timeout потоков = {roles.get('worker_thread')} — "
                    f"контракт storage: 15000 мс"})
    # известное проектное решение — НЕ дефект (этап 7 не меняет):
    out.append({"severity": "info", "finding":
                "busy_timeout=0 на соединении цикла событий — намеренный "
                "контракт параллельного storage-воркстрима: async-код не ждёт "
                "писателя в busy-handler, записи идут через DataSync._write "
                "(FIFO-поток, busy_timeout=15000). Поднимать busy_timeout на "
                "loop-соединении НЕЛЬЗЯ — это вернёт фризы цикла под "
                "потоковым расчётом."})
    out.append({"severity": "info", "finding":
                f"synchronous={p['synchronous']} "
                f"({SYNCHRONOUS_NAMES.get(p['synchronous'], '?')}) — дефолт "
                f"SQLite для WAL (FULL); durability не снижалась ни одним "
                f"этапом perf-спеки"})
    if report["wal_bytes"] and report["wal_bytes"] > 64 * 1024 * 1024:
        out.append({"severity": "info", "finding":
                    f"WAL {report['wal_bytes'] / 2**20:.1f} МиБ — "
                    f"автоматический checkpoint (wal_autocheckpoint="
                    f"{p['wal_autocheckpoint']} стр.) отстаёт; размер сожмётся "
                    f"после checkpoint при закрытии последнего соединения"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/htf_zones.db")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    db_path = Path(args.db)
    report = {"audited_at": int(time.time())}
    if db_path.exists():
        report.update(audit_db(db_path))
    else:
        report.update({"db_path": str(db_path), "error": "file not found"})
    report["connection_roles"] = probe_role_timeouts()
    report["findings"] = findings(report) if "pragmas" in report else []

    p = report.get("pragmas", {})
    print(f"БД: {report['db_path']}")
    if p:
        print(f"  sqlite {report['sqlite_version']}, "
              f"db {report['db_bytes']} B, WAL {report['wal_bytes']} B")
        for name in PRAGMAS:
            print(f"  {name:<18} {p[name]}")
        print(f"  строки: {report['row_counts']}")
    print("  busy_timeout по ролям соединений (зонд app.storage):")
    for role, value in report["connection_roles"].items():
        print(f"    {role:<16} {value} мс")
    print("  findings:")
    for f in report["findings"]:
        print(f"    [{f['severity']}] {f['finding']}")

    out = Path(args.out) if args.out else (
        Path("data/diag") / f"pragma_audit_{int(time.time())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"отчёт: {out}")


if __name__ == "__main__":
    main()
