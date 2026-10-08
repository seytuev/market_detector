"""Файл SQLite на томе Railway, а не в слое контейнера."""
from __future__ import annotations

from app.config import Settings, resolve_db_path


def test_relative_path_without_volume_stays():
    assert resolve_db_path("data/htf_zones.db", volume_mount="") == "data/htf_zones.db"


def test_memory_is_untouched():
    assert resolve_db_path(":memory:", volume_mount="/data") == ":memory:"


def test_relative_path_lands_on_volume():
    assert (
        resolve_db_path("data/htf_zones.db", volume_mount="/data")
        == "/data/htf_zones.db"
    )


def test_absolute_path_inside_volume_kept():
    assert (
        resolve_db_path("/data/htf_zones.db", volume_mount="/data/")
        == "/data/htf_zones.db"
    )


def test_absolute_path_outside_volume_moves():
    """Образ пишет в /data, том смонтирован в другой каталог — файл на томе."""
    assert (
        resolve_db_path("/data/htf_zones.db", volume_mount="/app/data")
        == "/app/data/htf_zones.db"
    )


def test_settings_follow_railway_volume(monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/vol")
    monkeypatch.setenv("HTF_DB_PATH", "data/htf_zones.db")
    assert Settings().db_path == "/vol/htf_zones.db"
