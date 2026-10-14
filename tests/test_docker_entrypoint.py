"""Каталоги, которые entrypoint готовит до открытия SQLite."""
import os
import sys
from types import SimpleNamespace

import pytest

from tools import docker_entrypoint
from tools.docker_entrypoint import volume_roots


def test_volume_roots_cover_mount_and_db_parent(monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    monkeypatch.setenv("HTF_DB_PATH", "/data/htf_zones.db")
    assert [str(path) for path in volume_roots()] == [os.path.normpath("/data")]


def test_volume_roots_keep_a_db_outside_the_mount(monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/volume")
    monkeypatch.setenv("HTF_DB_PATH", "/elsewhere/htf_zones.db")
    assert [str(path) for path in volume_roots()] == [
        os.path.normpath("/volume"),
        os.path.normpath("/elsewhere"),
    ]


def test_volume_roots_default_to_data(monkeypatch):
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.delenv("HTF_DB_PATH", raising=False)
    assert [str(path) for path in volume_roots()] == [os.path.normpath("/data")]


def test_dropping_root_sets_service_user_home_before_exec(monkeypatch):
    user = SimpleNamespace(pw_uid=10001, pw_gid=10001, pw_dir="/home/htf", pw_name="htf")
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _: user))
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda _: None, raising=False)
    monkeypatch.setattr(docker_entrypoint, "volume_roots", lambda: [])

    def execvp(command, argv):
        assert command == "python"
        assert argv == ["python", "-m", "app.main"]
        assert os.environ["HOME"] == "/home/htf"
        assert os.environ["USER"] == os.environ["LOGNAME"] == "htf"
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(os, "execvp", execvp)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        docker_entrypoint.main(["python", "-m", "app.main"])
