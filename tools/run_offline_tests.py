"""Run memory-only asyncio tests on hosts that cannot create loopback sockets.

This loop has no network I/O implementation. Production and network integration
tests must use the standard event loop in CI.
"""
import asyncio
import sys
import threading
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("HTF_TEST_TEMP_DIR", str(Path(__file__).resolve().parents[1] / "tmp" / "sqlite-tests"))


class _Waiter:
    def __init__(self):
        self.event = threading.Event()

    def select(self, timeout):
        if timeout != 0:
            self.event.wait(timeout)
        self.event.clear()
        return []

    def close(self):
        self.event.set()


class MemoryLoop(asyncio.BaseEventLoop):
    def __init__(self):
        super().__init__()
        self._selector = _Waiter()

    def _process_events(self, events):
        if events:
            raise AssertionError("Offline loop does not support I/O")

    def _write_to_self(self):
        self._selector.event.set()

    def close(self):
        self._selector.close()
        super().close()


class Policy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        return MemoryLoop()


if __name__ == "__main__":
    asyncio.set_event_loop_policy(Policy())
    import pytest
    raise SystemExit(pytest.main(["-p", "pytest_asyncio.plugin", *sys.argv[1:]]))
