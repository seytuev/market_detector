"""Thread-owned SQLite connections; a transaction never belongs to two callers."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
import os
import tempfile
from pathlib import Path


class Connection:
    def __init__(self, path: str):
        self.path = path
        self._memory_dir = None
        if path == ":memory:":
            base = os.environ.get("HTF_TEST_TEMP_DIR")
            if base:
                Path(base).mkdir(parents=True, exist_ok=True)
            self._memory_dir = Path(tempfile.mkdtemp(prefix="htf-sqlite-", dir=base)).resolve()
            self._target = str(self._memory_dir / "store.db")
        else:
            self._target = path
        self._uri = False
        self._local = threading.local()
        self._connections = []
        self._registry_lock = threading.Lock()
        self._closed = False
        self._conn  # keep shared memory alive
        self._conn.execute("PRAGMA journal_mode=WAL")

    @property
    def _lock(self):
        if not hasattr(self._local, "lock"):
            self._local.lock = threading.RLock()
        return self._local.lock

    @property
    def _conn(self):
        if self._closed:
            raise sqlite3.ProgrammingError("Database is closed")
        if not hasattr(self._local, "connection"):
            raw = sqlite3.connect(self._target, uri=self._uri, isolation_level=None,
                                  check_same_thread=False, timeout=15)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys=ON")
            self._local.connection = raw
            with self._registry_lock:
                self._connections.append(raw)
        raw = self._local.connection
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            timeout = 15000
        else:
            # Async callers must use to_thread for contended writes, not freeze
            # the event loop in SQLite's busy handler.
            timeout = 0
        if getattr(self._local, "timeout", None) != timeout:
            raw.execute(f"PRAGMA busy_timeout={timeout}")
            self._local.timeout = timeout
        return raw

    @property
    def unit_depth(self):
        return getattr(self._local, "unit_depth", 0)

    @unit_depth.setter
    def unit_depth(self, value):
        self._local.unit_depth = value

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        from .services.metrics import get_registry
        started = time.monotonic()
        kind = "read" if str(sql).lstrip().split(None, 1)[0].lower() in {"select", "pragma", "with", "explain"} else "write"
        try:
            return self._conn.execute(sql, *args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                get_registry().counter("sql_busy_errors", labels={"op": "execute"})
            raise
        finally:
            get_registry().histogram("sql_duration", time.monotonic()-started, {"op": "execute", "kind": kind})

    def executemany(self, *args, **kwargs):
        return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        if self.unit_depth:
            raise RuntimeError("executescript is not allowed inside a unit of work")
        return self._conn.executescript(*args, **kwargs)

    def commit(self):
        started = time.monotonic()
        if not self.unit_depth:
            self._conn.commit()
        from .services.metrics import get_registry
        get_registry().histogram("sql_duration", time.monotonic()-started,
                                 {"op": "commit", "kind": "write"})
        if self.path != ":memory:":
            wal = Path(self.path + "-wal")
            get_registry().gauge("db_wal_bytes", wal.stat().st_size if wal.exists() else 0)

    def rollback(self):
        if self.unit_depth:
            raise RuntimeError("Only the unit of work may roll back its transaction")
        self._conn.rollback()

    def close(self):
        with self._registry_lock:
            self._closed = True
            for raw in self._connections:
                raw.close()
            self._connections.clear()
            if self._memory_dir:
                # Remove only the four known files of the directory we created.
                for name in ("store.db", "store.db-wal", "store.db-shm", "store.db-journal"):
                    target = (self._memory_dir / name).resolve()
                    if target.parent != self._memory_dir:
                        raise RuntimeError("Invalid temporary SQLite path")
                    target.unlink(missing_ok=True)
                self._memory_dir.rmdir()
