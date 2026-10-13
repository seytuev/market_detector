"""Content identity for diagnostics, including uncommitted development builds."""
import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def source_revision():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py",".sql",".js",".css",".html"}:
            content = path.read_bytes()
            digest.update(path.relative_to(root).as_posix().encode()+b"\0")
            digest.update(len(content).to_bytes(8,"big"))
            digest.update(content)
    return digest.hexdigest()
