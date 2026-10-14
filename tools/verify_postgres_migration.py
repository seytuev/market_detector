"""Rehearse import on a private snapshot and isolated PostgreSQL test schema."""
import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlencode
import uuid

import psycopg
from psycopg import sql

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import Database
from app.services.postgres_migration import import_sqlite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("--report",type=Path)
    args = parser.parse_args()
    url = os.environ.get("HTF_TEST_POSTGRES_URL", "")
    if not url:
        parser.error("Set HTF_TEST_POSTGRES_URL to a disposable test server")
    logging.basicConfig(level=logging.INFO)
    schema = "verify_"+uuid.uuid4().hex
    with psycopg.connect(url,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    target = url+("&" if "?" in url else "?")+urlencode({"options":"-csearch_path="+schema})
    started = time.monotonic()
    db = None
    try:
        with tempfile.TemporaryDirectory(prefix="postgres-rehearsal-") as temp:
            snapshot = Path(temp)/"source.db"
            source = sqlite3.connect(args.source.resolve().as_uri()+"?mode=ro",uri=True)
            copy = sqlite3.connect(snapshot)
            try:
                source.backup(copy)
                copy.execute("UPDATE meta SET value='{}' WHERE key IN ('runtime:calc_owner','runtime:telegram_owner','settings:owner')")
                copy.commit()
            finally:
                source.close()
                copy.close()
            db = Database(target)
            tables = import_sqlite(snapshot,db)
            report = {"tables":tables,"total_rows":sum(t["rows"] for t in tables.values()),"elapsed_seconds":round(time.monotonic()-started,2)}
            if args.report:
                args.report.parent.mkdir(parents=True,exist_ok=True)
                args.report.write_text(json.dumps(report,indent=2),encoding="utf-8")
            print(f"Verified {len(tables)} tables, {report['total_rows']} rows in {report['elapsed_seconds']} seconds")
    finally:
        if db:
            db.close()
        with psycopg.connect(url,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
