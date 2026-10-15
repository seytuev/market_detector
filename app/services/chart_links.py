"""Persistent, unguessable links to chart images; never expose owner tokens."""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import tempfile
from pathlib import Path

from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_directory(settings) -> Path:
    return Path(settings.db_path).parent / "shared_charts"


def save_chart_image(settings, data: bytes) -> str:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Размер изображения должен быть до 10 МБ.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "PNG" or img.width * img.height > 32_000_000:
                raise ValueError("Нужен PNG размером до 32 мегапикселей.")
            img.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Не удалось прочитать PNG.") from exc
    # Keyed digest deduplicates a snapshot without making its URL guessable.
    key = hmac.new(settings.auth_token.encode(), data, hashlib.sha256).hexdigest()
    directory = image_directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.png"
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
            tmp.write(data)
            temporary = Path(tmp.name)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return f"/charts/{key}.png"


def chart_image_path(settings, key: str) -> Path | None:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        return None
    path = image_directory(settings) / f"{key}.png"
    return path if path.is_file() else None
