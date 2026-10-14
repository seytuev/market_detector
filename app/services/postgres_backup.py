"""Consistent PostgreSQL COPY backups, without a client/server version mismatch."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from psycopg import sql

from ..models import now_ms
from ..postgres import SCHEMA_VERSION


def export_database(db, destination):
    destination = Path(destination)
    raw = db.conn._conn.raw
    tables = {}
    with db.read_tx():
        profile = db.get_meta("settings:active_profile")
        state = db.get_state_seq()
        names = raw.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' AND table_name<>'postgres_schema_version' ORDER BY table_name").fetchall()
        for (name,) in names:
            fields = raw.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (name,)).fetchall()
            columns = [r[0] for r in fields]
            query = sql.SQL("COPY {} ({}) TO STDOUT").format(sql.Identifier(name), sql.SQL(",").join(map(sql.Identifier, columns)))
            digest = hashlib.sha256()
            filename = name+".copy.gz"
            with gzip.open(destination/filename,"wb") as output, raw.cursor().copy(query) as copy:
                for block in copy:
                    output.write(block)
                    digest.update(block)
            tables[name] = {"file":filename,"columns":columns,"types":[r[1] for r in fields],"sha256":digest.hexdigest()}
    schema = Path(__file__).resolve().parents[1]/"schema_postgres.sql"
    (destination/"schema_postgres.sql").write_bytes(schema.read_bytes())
    manifest = {"created_at":now_ms(), "state_version":state, "database":"postgresql", "schema_version":SCHEMA_VERSION,"tables":tables, "media_optional":True}
    return manifest, profile


def restore_database(db, directory):
    """Restore only into an empty target, with checksum validation and rollback."""
    directory = Path(directory)
    manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    if manifest["database"] != "postgresql" or manifest["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("Unsupported PostgreSQL backup version")
    raw = db.conn._conn.raw
    with raw.transaction():
        raw.execute("SELECT pg_advisory_xact_lock(741852965)")
        from .postgres_migration import is_archive, create_archive
        for name,table in manifest["tables"].items():
            if is_archive(name):
                create_archive(raw,name,list(zip(table["columns"],table["types"])))
        for name in manifest["tables"]:
            if raw.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(name))).fetchone():
                raise RuntimeError("Restore target must be empty")
        raw.execute("SET CONSTRAINTS ALL DEFERRED")
        for name, table in manifest["tables"].items():
            source = (directory/table["file"]).resolve()
            if source.parent != directory.resolve():
                raise RuntimeError("Invalid backup path")
            digest = hashlib.sha256()
            query = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(name),sql.SQL(",").join(map(sql.Identifier,table["columns"])))
            with gzip.open(source,"rb") as data, raw.cursor().copy(query) as copy:
                while block := data.read(1024*1024):
                    digest.update(block)
                    copy.write(block)
            if digest.hexdigest() != table["sha256"]:
                raise RuntimeError(f"Backup checksum failed: {name}")
        for name in db.conn._id_tables:
            raw.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,'id'),COALESCE(MAX(id),1),MAX(id) IS NOT NULL) FROM {}").format(sql.Identifier(name)),(name,))
    db._drop_read_caches()
