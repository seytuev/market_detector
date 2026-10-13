"""Каталоги, которые entrypoint готовит до открытия SQLite."""
import os

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
