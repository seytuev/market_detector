"""CLI-обёртка миграции §13 ТЗ 07.10.2026 (логика — app/services/
taken_levels_migration.py, чтобы воркер выполнял её при старте и в Docker).

python tools/migrate_taken_levels.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import os

from app.services.taken_levels_migration import (  # noqa: F401 — реэкспорт
    MIGRATION_KEY,
    run,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get(
        "HTF_DB_PATH", "data/htf_zones.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rep = run(args.db, dry_run=args.dry_run)
    c = rep["counters"]
    print(f"миграция taken_levels: "
          f"{'DRY-RUN' if rep['dry_run'] else 'ВЫПОЛНЕНО'}; счётчики: {c}")


if __name__ == "__main__":
    main()
