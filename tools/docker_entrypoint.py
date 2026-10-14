"""Prepare the data directory, then start the service as user htf.

Railway mounts the volume as root. The previous image ran as root and left
the database owned by root. The process itself must not stay root, but it
cannot open that volume until ownership is fixed. WAL also creates sibling
files next to the database, so the directory has to be writable, not only
the database file.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def volume_roots() -> list[Path]:
    """Directories that must be writable before SQLite opens the database."""
    roots: list[Path] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        cleaned = (raw or "").strip().rstrip("/\\")
        if not cleaned:
            return
        text = os.path.normpath(cleaned)
        key = os.path.normcase(text)
        if key in seen:
            return
        seen.add(key)
        roots.append(Path(text))

    add(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ""))
    db = os.environ.get("HTF_DB_PATH", "/data/htf_zones.db").strip()
    if db and db != ":memory:":
        add(str(Path(db).parent))
    if not roots:
        add("/data")
    return roots


def _own(path: Path, uid: int, gid: int) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        os.lchown(path, uid, gid)
        return
    os.chown(path, uid, gid)
    mode = stat.S_IMODE(info.st_mode)
    mode |= 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
    os.chmod(path, mode)


def prepare_tree(root: Path, uid: int, gid: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _own(root, uid, gid)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in dirnames:
            _own(current / name, uid, gid)
        for name in filenames:
            _own(current / name, uid, gid)


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("docker entrypoint: command is missing")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        import pwd
        user = pwd.getpwnam("htf")
        for root in volume_roots():
            prepare_tree(root, user.pw_uid, user.pw_gid)
            print(f"каталог базы доступен uid {user.pw_uid}: {root}", flush=True)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        os.environ["HOME"] = user.pw_dir
        os.environ["USER"] = user.pw_name
        os.environ["LOGNAME"] = user.pw_name
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
