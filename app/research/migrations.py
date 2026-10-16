"""Research schema is migrated at startup, never by a read endpoint."""
from pathlib import Path


def migrate(db):
    script = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with db.batch_writes():
        if db.is_postgres:
            db.conn.execute("SELECT pg_advisory_xact_lock(741852967)")
        db.conn.execute("CREATE TABLE IF NOT EXISTS research_schema_version(version BIGINT PRIMARY KEY)")
        row = db.conn.execute("SELECT MAX(version) FROM research_schema_version").fetchone()
        if row[0] and row[0] > 5:
            raise RuntimeError("Research schema is newer than this application")
        if not row[0]:
            for statement in script.split(";"):
                if statement.strip():
                    db.conn.execute(statement)
            db.conn.execute("INSERT INTO research_schema_version(version) VALUES(1)")
        if not row[0] or row[0] < 2:
            extra=Path(__file__).with_name("schema_v2.sql").read_text(encoding="utf-8")
            for statement in extra.split(";"):
                if statement.strip():
                    db.conn.execute(statement)
            db.conn.execute("INSERT INTO research_schema_version(version) VALUES(2)")
        if not row[0] or row[0] < 3:
            if row[0]:
                db.conn.execute("ALTER TABLE research_analysis ADD COLUMN change_seq BIGINT NOT NULL DEFAULT 0")
            db.conn.execute("INSERT INTO research_schema_version(version) VALUES(3)")
        if not row[0] or row[0] < 4:
            for statement in Path(__file__).with_name("schema_v4.sql").read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    db.conn.execute(statement)
            db.conn.execute("INSERT INTO research_schema_version(version) VALUES(4)")
        if not row[0] or row[0] < 5:
            for statement in Path(__file__).with_name("schema_v5.sql").read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    db.conn.execute(statement)
            db.conn.execute("INSERT INTO research_schema_version(version) VALUES(5)")
