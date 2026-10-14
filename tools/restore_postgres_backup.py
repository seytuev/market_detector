"""Restore a verified COPY backup into an empty PostgreSQL database."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import Database
from app.services.postgres_backup import restore_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    target = os.environ.get("DATABASE_URL", "")
    if not target.startswith(("postgres://","postgresql://")):
        parser.error("Set DATABASE_URL to an empty PostgreSQL target")
    db = Database(target)
    try:
        restore_database(db,args.directory)
        print("Backup restored and verified")
    finally:
        db.close()


if __name__ == "__main__":
    main()
