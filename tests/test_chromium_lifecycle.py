"""Screenshot subprocesses cannot accumulate after completion or timeout."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.notify import h1_image


def test_renderer_collects_output_and_exit_code():
    result = h1_image._run_chromium(
        [sys.executable, "-c", "import sys; print('rendered'); sys.exit(7)"])
    assert result.stdout.strip() == "rendered"
    assert result.returncode == 7


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("group_exists", [False, True])
def test_posix_renderer_cleans_descendants_and_reaps_parent(monkeypatch, timeout, group_exists):
    calls = []

    def communicate(**kwargs):
        calls.append(("communicate", kwargs))
        if timeout and kwargs:
            raise subprocess.TimeoutExpired("chromium", 30)
        return "rendered", ""

    process = SimpleNamespace(pid=23456, returncode=0, communicate=communicate)

    def popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        return process

    def killpg(pid, sig):
        calls.append(("killpg", pid, sig))
        if not group_exists:
            raise ProcessLookupError()

    monkeypatch.setattr(h1_image, "os", SimpleNamespace(name="posix", killpg=killpg))
    monkeypatch.setattr(h1_image, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(h1_image.subprocess, "Popen", popen)
    if timeout:
        with pytest.raises(subprocess.TimeoutExpired):
            h1_image._run_chromium(["chromium"])
    else:
        assert h1_image._run_chromium(["chromium"]).stdout == "rendered"
    assert calls == [
        ("communicate", {"timeout": 30}),
        ("killpg", process.pid, 9),
        ("communicate", {}),
    ]
