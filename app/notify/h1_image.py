"""Notification PNGs using the website's chart library and light theme.

The browser renders an offline document: no server, credentials or network
requests are involved. Only closed candles and layers at occurred_at are used.
"""
from __future__ import annotations

import json
from html import unescape
import os
from pathlib import Path
import shutil
import re
import subprocess
import tempfile
from threading import Lock

from .chart_series import screenshot_candles
from .chart_snapshot import KIND_RU
from .formatting import fmt_time_msk

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"
WIDTH, HEIGHT = 1920, 960
_RENDER_LOCK = Lock()


def chromium_path() -> str:
    configured = os.environ.get("HTF_CHROMIUM_PATH")
    if configured:
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if root:
            for relative in ("Google/Chrome/Application/chrome.exe",
                             "Microsoft/Edge/Application/msedge.exe"):
                candidate = Path(root) / relative
                if candidate.is_file():
                    return str(candidate)
    raise RuntimeError("Для снимка графика нужен Chromium (HTF_CHROMIUM_PATH)")


def event_snapshot(db, observation_id: int, settings, event, *, period_days=7):
    """Website read model frozen at the event, with context before the local leg."""
    obs = db.get_ltf_observation(observation_id)
    if obs is None:
        return None
    moment = int(event.occurred_at)
    candles = screenshot_candles(
        db, obs.instrument_id, "H1", end_ms=moment, now=moment,
        start_ms=moment - max(7, min(14, period_days)) * 86_400_000,
    )
    if not candles:
        return None
    from ..services.h1_chart import assemble_h1_layers

    layers = assemble_h1_layers(
        db, settings, obs.instrument_id, as_of=moment,
        window_from=candles[0].open_time, window_to=moment,
        context_id=observation_id, setup_event=True,
    )
    instrument = db.get_instrument(obs.instrument_id)
    return {
        "title": f"{instrument.symbol if instrument else 'H1'} · H1 · "
                 f"{KIND_RU.get(event.kind, event.kind)}",
        "subtitle": f"LevelFrame · {fmt_time_msk(moment)} · снимок события",
        "as_of": moment,
        "timeframe": "H1",
        "event": {"kind": event.kind, "payload": event.payload or {}},
        "candles": candle_rows(candles),
        "layers": layers,
    }


def snapshot_html(snapshot: dict) -> str:
    # Escape '<' even inside JSON strings: payload text cannot close a script.
    data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    template = (STATIC / "h1_snapshot.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    scripts = "\n".join(
        "<script>" + (STATIC / name).read_text(encoding="utf-8") + "</script>"
        for name in ("vendor/lightweight-charts.js", "common.js", "h1_layers.js")
    )
    # Replace data last so user text cannot be interpreted as a template token.
    return (template.replace("<!-- SHARED_STYLE -->", "<style>" + css + "</style>")
            .replace("<!-- SHARED_SCRIPTS -->", scripts)
            .replace("/* SNAPSHOT_RENDERER */", (STATIC / "h1_snapshot.js").read_text(encoding="utf-8"))
            .replace("/* SNAPSHOT_DATA */", data))


def render_snapshot(snapshot: dict, out_path) -> str:
    """One bounded Chromium process; failed renders never overwrite a valid PNG."""
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Notifications can arrive in batches; bound browser memory to one process.
    with _RENDER_LOCK, tempfile.TemporaryDirectory(prefix="lf-h1-") as folder:
        work = Path(folder)
        html = work / "chart.html"
        png = work / "chart.png"
        html.write_text(snapshot_html(snapshot), encoding="utf-8")
        command = [
            chromium_path(), "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--window-size={WIDTH},{HEIGHT}",
            "--virtual-time-budget=2500", "--timeout=20000",
            f"--user-data-dir={work / 'profile'}", f"--screenshot={png}",
            "--dump-dom", html.as_uri(),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ready = re.search(r'<html\b[^>]*\bdata-rendered="true"', result.stdout)
        if result.returncode or not ready or not png.is_file():
            error = re.search(r'<html\b[^>]*\bdata-render-error="([^"]*)"', result.stdout)
            detail = unescape(error[1])[:300] if error else "рендер не завершён"
            raise RuntimeError(f"Не удалось отрисовать снимок графика (Chromium {result.returncode}): {detail}")
        if png.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("Chromium не создал PNG графика H1")
        # The temporary directory can be on another filesystem than /data.
        staged = out.with_suffix(".tmp.png")
        shutil.copyfile(png, staged)
        staged.replace(out)
    return str(out)


def render_h1_event_chart(db, observation_id, out_path, *, settings, event, period_days=7):
    snapshot = event_snapshot(db, observation_id, settings, event, period_days=period_days)
    return render_snapshot(snapshot, out_path) if snapshot else None


def candle_rows(candles):
    return [{"time": c.open_time // 1000, "open": c.open, "high": c.high,
             "low": c.low, "close": c.close} for c in candles]


def zone_snapshot(candles, zone, source_label, *, event_at=None, event_price=None):
    """HTF notification scene; the dispatcher owns live/event candle selection."""
    if not candles:
        raise ValueError("Нужны свечи для снимка зоны")
    from ..texts_ru import DIRECTION_RU, STATUS_RU, TYPE_RU
    from .formatting import fmt_price_ru

    symbol = source_label.split("/")[-1].strip() or source_label
    moment = event_at if event_at is not None else candles[-1].close_time
    kind = zone.type.value.upper()
    arrow = "↑" if zone.direction.value == "bull" else "↓"
    label = f"{kind} {zone.timeframe}" + ("" if zone.is_level else f" {arrow}")
    return {
        "title": f"{symbol} · {TYPE_RU.get(zone.type.value, kind)} {zone.timeframe}",
        "subtitle": f"LevelFrame · {fmt_time_msk(moment)} · снимок события",
        "timeframe": zone.timeframe, "as_of": moment,
        "candles": candle_rows(candles),
        "overlays": [{"from": zone.display_from if zone.display_from is not None else zone.formed_at,
                      "lower": zone.lower, "upper": zone.upper, "mid": zone.mid,
                      "type": kind, "direction": zone.direction.value, "label": label,
                      "muted": not zone.is_currently_relevant()}],
        "levels": ([{"price": event_price, "label": "Цена события", "from": candles[0].open_time}]
                   if event_price is not None else []),
        "caption": f"{STATUS_RU.get(zone.status.value, zone.status.value)} · "
                   f"{DIRECTION_RU.get(zone.direction.value, zone.direction.value)} · "
                   f"{fmt_price_ru(zone.lower)}–{fmt_price_ru(zone.upper)}",
    }


def render_zone_notification(candles, zone, out_path, source_label, *, event_at=None, event_price=None):
    return render_snapshot(zone_snapshot(candles, zone, source_label,
                           event_at=event_at, event_price=event_price), out_path)
