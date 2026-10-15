"""Render five explicit examples, never fetch or send live market notifications."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.events.notification import build_notice, notice_text
from app.events.notification_image import notice_html, render_notice_image
from app.events.timeutil import MSK


def example(direction="neutral"):
    assessment = {
        "assessment_id": "asmt_1dbb49ccc8cf122b5866" if direction == "neutral" else "preview-" + direction,
        "evaluated_at": int(datetime(2026, 10, 15, 22, 5, tzinfo=MSK).timestamp() * 1000),
        "direction": direction, "events_direction": direction, "time_direction": "neutral",
        "time_rules_active": False, "quality": "ok", "coverage": {"ok": 20, "total": 20},
        "methodology": {"label": "Экспериментальная модель корзины"},
        "active_facts": [{"fact_key": "example", "context_status": "awaiting_confirmation"}],
    }
    pubs = [{"kind": "direction", "payload": {"from": "short" if direction != "short" else "neutral", "to": direction}},
            {"kind": "fact_reinterpreted", "payload": {"fact_key": "example", "direction": None}}]
    if direction in ("long", "short"):
        assessment["reasons"] = [{"side": direction, "rule_id": "LIQ_RECOVERY" if direction == "long" else "LIQ_PRESSURE"}]
    elif direction == "mixed":
        assessment.update(contradictory=True, quality="partial", reasons=[
            {"side": "long", "rule_id": "LIQ_RECOVERY"}, {"side": "short", "rule_id": "LIQ_PRESSURE"}])
    elif direction == "unknown":
        assessment.update(quality="unavailable", coverage=None, unknown_dependencies=["price", "liquidations"], active_facts=[])
    return build_notice(assessment, pubs, {}, base_url="https://marketdetector-production.up.railway.app")


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tmp/market-notification").resolve()
    out.mkdir(parents=True, exist_ok=True)
    for direction in ("neutral", "long", "short", "mixed", "unknown"):
        notice = example(direction)
        png = Path(render_notice_image(notice, out))
        (out / f"{direction}.png").write_bytes(png.read_bytes())
        (out / f"{direction}.txt").write_text(notice_text(notice), encoding="utf-8")
        (out / f"{direction}.html").write_text(notice_html(notice), encoding="utf-8")
        (out / f"{direction}.json").write_text(json.dumps(notice, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{direction}: {out / (direction + '.png')}")


if __name__ == "__main__":
    main()
