"""Serve an isolated SQLite snapshot for before/after LTF browser measurements.

Create tmp/ltf-zone-bench/baseline/app and market.db before changing sources.
No market worker, notifications, or writes to the source database are started.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--port', type=int, default=8123)
    parser.add_argument('--snapshot-time', action='store_true', help='Freeze application time at the latest closed H1 of instrument 1')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    fixture = root / 'tmp' / 'ltf-zone-bench'
    sys.path.insert(0, str(fixture / 'baseline' if args.baseline else root))
    from app.config import Settings
    from app.db import Database
    from app.web.api import create_app
    import uvicorn

    settings = Settings()
    settings.db_path = str(fixture / 'market.db')
    settings.database_url = ''
    settings.auth_token = 'zone-bench-token'
    db = Database(settings.db_path)
    if args.snapshot_time:
        import app.models as models
        moment = db.last_candle(1, 'H1').close_time + 1
        original = models.now_ms
        fixed_now = lambda: moment
        # Replace application aliases only. Browser/server performance clocks
        # and event-loop deadlines continue running normally.
        for name, module in list(sys.modules.items()):
            if name == 'app' or name.startswith('app.'):
                if getattr(module, 'now_ms', None) is original:
                    module.now_ms = fixed_now
        print(f'Application snapshot time: {moment}', flush=True)
    app = create_app(db, settings)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
