"""Offline branded market cards, rendered from the same model as the caption."""
from __future__ import annotations

import base64
import hashlib
from html import escape
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Lock

from ..notify.h1_image import chromium_path

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"
WIDTH, HEIGHT = 1080, 1080
_LOCK = Lock()


def _valid_image(path: Path) -> bool:
    from PIL import Image
    try:
        with Image.open(path) as image:
            valid = image.format == "PNG" and image.width == WIDTH and 300 <= image.height <= HEIGHT
            image.verify()
            return valid
    except (OSError, ValueError):
        return False


def notice_html(notice: dict) -> str:
    template = (STATIC / "market_notice.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    logo = base64.b64encode((STATIC / "brand" / "stv-logo-v2.png").read_bytes()).decode("ascii")
    fields = {**notice, "reason_heading": "После перерыва" if notice["mode"] == "digest" else "Сейчас в контексте",
              "footer": "Экспериментальная модель корзины" if notice["experimental"] else "Снимок рыночной оценки",
              "mode_label": {"digest": "После перерыва", "updated": "Актуализированная сводка"}.get(notice["mode"], "Обновление рынка")}
    reasons = "".join('<li><span class="notice-bullet"></span><span>' + escape(text) + '</span></li>' for text in notice["reasons"])
    template = (template.replace("<!-- SHARED_STYLE -->", "<style>" + css + "</style>")
                .replace("/* BRAND_LOGO */", "data:image/png;base64," + logo))
    # One-pass substitution: source text cannot become a second template token.
    return re.sub(r"{{([a-z_]+)}}|<!-- NOTICE_REASONS -->",
                  lambda m: escape(str(fields.get(m[1], ""))) if m[1] else reasons, template)


def render_notice_image(notice: dict, directory) -> str:
    html = notice_html(notice)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:24]
    out = Path(directory).resolve() / f"market-{digest}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if out.is_file() and _valid_image(out):
            return str(out)
        with tempfile.TemporaryDirectory(prefix="stv-market-") as folder:
            work = Path(folder)
            page, png = work / "market.html", work / "market.png"
            page.write_text(html, encoding="utf-8")
            result = subprocess.run([
                chromium_path(), "--headless", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--disable-background-networking",
                "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
                "--force-device-scale-factor=1", f"--window-size={WIDTH},{HEIGHT}",
                "--virtual-time-budget=2000", "--timeout=15000",
                f"--user-data-dir={work / 'profile'}", f"--screenshot={png}",
                "--dump-dom", page.as_uri(),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready = re.search(r'<html\b[^>]*\bdata-rendered="true"', result.stdout)
            if result.returncode or not ready or not png.is_file() or png.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("Не удалось отрисовать карточку рынка: логотип не загружен или текст не помещается")
            height_match = re.search(r'<html\b[^>]*\bdata-image-height="(\d+)"', result.stdout)
            if not height_match or not 300 <= int(height_match[1]) <= HEIGHT:
                raise RuntimeError("Не получена высота карточки рынка")
            # Same filesystem: a failed/incomplete render never replaces the cache.
            with tempfile.NamedTemporaryFile(dir=out.parent, suffix=".png", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                from PIL import Image
                with Image.open(png) as rendered:
                    rendered.crop((0, 0, WIDTH, int(height_match[1]))).save(temporary, format="PNG")
                temporary.replace(out)
            finally:
                temporary.unlink(missing_ok=True)
    return str(out)
