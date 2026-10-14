"""One-time, atomic import of a quiescent SQLite database into PostgreSQL."""
from __future__ import annotations

import hashlib
from contextlib import ExitStack, closing
import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from psycopg import sql

from ..db import Database
from ..models import now_ms

log = logging.getLogger(__name__)
IMPORT_KEY = "migration:sqlite_to_postgres:v1"
ARCHIVE_SUFFIXES = ("_bak_ltfmig","_bak_ltfv2")
ARCHIVE_TABLES = {"ltf_migration_log"}


def is_archive(name):
    return name.endswith(ARCHIVE_SUFFIXES) or name in ARCHIVE_TABLES


def create_archive(raw, name, columns):
    """Historical tables retain every value, without joining live constraints."""
    allowed = {"bigint","double precision","text","bytea","numeric"}
    if not is_archive(name) or any(kind not in allowed for _,kind in columns):
        raise RuntimeError(f"Unsupported legacy archive table: {name}")
    fields = sql.SQL(",").join(sql.SQL("{} {}").format(sql.Identifier(column),sql.SQL(kind)) for column,kind in columns)
    raw.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(sql.Identifier(name),fields))


def _mark_source(source_path, receipt):
    marker = Path(str(source_path)+".postgres-migrated")
    if not Path(source_path).is_file():
        return
    data = {"import_id":receipt["import_id"], "completed_at":receipt["completed_at"]}
    temporary = marker.with_name(marker.name+".tmp")
    temporary.write_text(json.dumps(data),encoding="utf-8")
    temporary.replace(marker)


def _digest(rows):
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def import_sqlite(source_path, db):
    """Caller holds the PostgreSQL import lock; target must contain no data.

    Legacy schema upgrades run on a private backup, never on the original.
    All rows, identity sequences and the import receipt commit together.
    """
    with db.conn.session():
        source_path = Path(source_path).resolve()
        if not source_path.is_file():
            raise RuntimeError("SQLite import source does not exist")
        raw = db.conn._conn.raw
        tables = [r[0] for r in raw.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' AND table_name<>'postgres_schema_version' ORDER BY table_name")]
        for table in tables:
            if raw.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))).fetchone():
                raise RuntimeError(f"PostgreSQL target is not empty: {table}; refusing partial import")
        with tempfile.TemporaryDirectory(prefix="postgres-import-", dir=source_path.parent) as temp, ExitStack() as resources:
            snapshot = Path(temp) / "snapshot.db"
            original = sqlite3.connect(source_path.as_uri()+"?mode=ro", uri=True)
            try:
                # Refuse to cut over while the old calculation owner is still live.
                deadline = time.monotonic()+180
                while True:
                    claims = original.execute("SELECT value FROM meta WHERE key IN ('runtime:calc_owner','runtime:telegram_owner','settings:owner')").fetchall()
                    live = any(isinstance(body := json.loads(r[0] or "{}"), dict) and body.get("expires_at", 0)>now_ms() for r in claims)
                    if not live:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("SQLite has an active runtime lease; stop the old application before importing")
                    time.sleep(1)
                guard = resources.enter_context(closing(sqlite3.connect(source_path, timeout=15,isolation_level=None)))
                guard.execute("BEGIN IMMEDIATE")  # fence any late legacy writer until cutover completes
                with sqlite3.connect(snapshot) as target:
                    original.backup(target)
                target.close()
            finally:
                original.close()
            with sqlite3.connect(snapshot) as integrity:
                if integrity.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite source integrity check failed")
            integrity.close()
            upgraded = Database(str(snapshot))
            upgraded.close()
            from .taken_levels_migration import run
            run(snapshot)
            source = sqlite3.connect(snapshot)
            try:
                source_tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                extra = source_tables-set(tables)
                unmapped = {table for table in extra if not is_archive(table)}
                if unmapped:
                    raise RuntimeError(f"SQLite contains unmapped tables: {sorted(unmapped)}")
                manifest = {}
                with raw.transaction():
                    raw.execute("SET CONSTRAINTS ALL DEFERRED")
                    kinds = {"INTEGER":"bigint","INT":"bigint","REAL":"double precision","TEXT":"text","BLOB":"bytea","NUMERIC":"numeric"}
                    for table in sorted(extra):
                        fields = [(r[1],kinds.get(r[2].upper(),"unsupported")) for r in source.execute(f'PRAGMA table_info("{table}")')]
                        create_archive(raw,table,fields)
                    tables.extend(sorted(extra))
                    for table in tables:
                        if table not in source_tables:
                            continue
                        info = source.execute(f'PRAGMA table_info("{table}")').fetchall()
                        columns = [r[1] for r in info]
                        order = [r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]] or columns
                        text_columns = {r[1] for r in info if r[2].upper()=="TEXT"}
                        query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table),
                            sql.SQL(",").join(sql.SQL('{} COLLATE "C" NULLS FIRST' if c in text_columns else "{} NULLS FIRST").format(sql.Identifier(c)) for c in order))
                        selection = f'SELECT {",".join(chr(34)+c+chr(34) for c in columns)} FROM "{table}" ORDER BY {",".join(chr(34)+c+chr(34) for c in order)}'
                        copy_query = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)))
                        with raw.cursor().copy(copy_query) as copy:
                            for row in source.execute(selection):
                                copy.write_row(tuple(row))
                        # Stream both sides so verification is bounded by a row.
                        expected = _digest(source.execute(selection))
                        with raw.cursor(name="verify_import") as cursor:
                            cursor.execute(query)
                            actual = _digest(cursor)
                        if expected != actual:
                            raise RuntimeError(f"Import verification failed for {table}")
                        manifest[table] = {"rows":expected[0], "sha256":expected[1]}
                        log.info("PostgreSQL import: %s, %s rows verified", table, expected[0])
                    for table in db.conn._id_tables:
                        raw.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {}").format(sql.Identifier(table)), (table,))
                    # Old-process leases cannot own the new database. Historical
                    # delivery and outbox state is deliberately preserved.
                    raw.execute("DELETE FROM meta WHERE key IN ('runtime:calc_owner','runtime:telegram_owner','settings:owner')")
                    receipt = {"import_id":uuid.uuid4().hex,"completed_at":now_ms(), "tables":manifest}
                    raw.execute("INSERT INTO meta(key,value) VALUES (%s,%s)", (IMPORT_KEY, json.dumps(receipt)))
                raw.execute("ANALYZE")
                _mark_source(source_path,receipt)
                return manifest
            finally:
                source.close()

def ensure_postgres(settings):
    if not settings.database_url:
        return
    if not settings.database_url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL")
    db = Database(settings.database_url)
    with db.conn.session():
        raw = db.conn._conn.raw
        try:
            raw.execute("SELECT pg_advisory_lock(741852965)")
            receipt = db.get_meta(IMPORT_KEY)
            marker = Path(settings.db_path+".postgres-migrated")
            if receipt:
                receipt = json.loads(receipt)
                if marker.exists() and json.loads(marker.read_text(encoding="utf-8"))["import_id"] != receipt.get("import_id"):
                    raise RuntimeError("PostgreSQL import receipt does not match the SQLite source")
                if "import_id" in receipt:
                    _mark_source(settings.db_path,receipt)
                return
            if marker.exists():
                raise RuntimeError("SQLite was already migrated; refusing to import a stale source into another PostgreSQL target")
            if Path(settings.db_path).is_file():
                import_sqlite(settings.db_path, db)
            else:
                # A missing source is expected only for genuinely new installations.
                if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
                    raise RuntimeError("SQLite source missing on Railway volume; refusing to start an empty PostgreSQL database")
                db.set_meta(IMPORT_KEY, json.dumps({"completed_at":now_ms(), "fresh_install":True}))
        finally:
            raw.execute("SELECT pg_advisory_unlock(741852965)")
            db.close()
