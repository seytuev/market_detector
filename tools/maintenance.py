"""Offline administration: backup, verify and conservative retention."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import Database
from app.services.maintenance import backup, retain

parser = argparse.ArgumentParser()
parser.add_argument("operation", choices=["backup", "verify", "retain"])
parser.add_argument("--db", required=True)
parser.add_argument("--destination")
args = parser.parse_args()
db = Database(args.db)
try:
    if args.operation == "backup":
        if not args.destination:
            parser.error("--destination required")
        result = backup(db,args.destination)
    elif args.operation == "verify":
        result = {"integrity_check":db.conn.execute("PRAGMA integrity_check").fetchone()[0],"foreign_key_errors":[dict(r) for r in db.conn.execute("PRAGMA foreign_key_check")]}
    else:
        result = retain(db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
finally:
    db.close()
