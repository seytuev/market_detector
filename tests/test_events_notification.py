"""Market notices: coherent snapshots, safe media and Telegram delivery shape."""
from types import SimpleNamespace

import pytest
from PIL import Image

from app.events import notification_image, publish, store
from app.events.notification import STATUS, build_notice, notice_text
from app.notify.navigation import card_keyboard
from tools.preview_market_notifications import example
from tests.test_events_publish import _asmt, _settings, T0


@pytest.mark.parametrize("direction", list(STATUS))
def test_five_states_render_readable_branded_images(direction, tmp_path):
    notice = example(direction)
    html = notification_image.notice_html(notice)
    assert notice["title"] in html and notice["stamp"] in html
    assert "data:image/png;base64," in html
    assert "connect-src 'none'" in html
    image_path = notification_image.render_notice_image(notice, tmp_path)
    with Image.open(image_path) as image:
        assert image.width == 1080 and 500 <= image.height <= 1080
        image.verify()
    assert len(notice_text(notice).encode("utf-16-le")) // 2 < 1000


def test_bundle_collapses_intermediate_directions_and_fact_phases(db):
    assessment = _asmt(direction="neutral", events="neutral", aid="final")
    assessment["active_facts"] = [{"fact_key": "cascade", "context_status": "awaiting_confirmation"}]
    store.insert_assessment(db, assessment)
    bundle = store.create_bundle(db, opened_at=T0, closes_at=T0 + 60_000)
    for i, (kind, payload) in enumerate([
        ("direction", {"from": "short", "to": "long", "text": "SHORT → LONG"}),
        ("fact_appeared", {"fact_key": "cascade", "direction": "short", "text": "Новое событие: SHORT"}),
        ("fact_reinterpreted", {"fact_key": "cascade", "direction": "long", "text": "теперь LONG"}),
        ("direction", {"from": "long", "to": "neutral", "text": "LONG → Нейтрально"}),
        ("fact_reinterpreted", {"fact_key": "cascade", "direction": None, "text": "теперь без направления"}),
    ]):
        store.add_publication(db, pub_key=f"notice:{i}", bundle_id=bundle, kind=kind,
                              assessment_id="final", payload=payload, created_at=T0 + i, window_ms=None)
    text = publish.render_bundle_text(db, bundle, "https://example.com")
    assert "Снижение → Без перевеса" in text and "Прежнее давление снято" in text
    assert "LONG" not in text and "SHORT" not in text
    details = publish.render_bundle_details(db, bundle)
    assert "теперь LONG" in details and "SHORT → LONG" in details
    assert len(store.bundle_publications(db, bundle)) == 5


def test_round_trip_direction_does_not_claim_net_change():
    assessment = {"direction": "short"}
    pubs = [{"kind": "direction", "payload": {"from": "short", "to": "long"}},
            {"kind": "direction", "payload": {"from": "long", "to": "short"}}]
    notice = build_notice(assessment, pubs, {})
    assert notice["change_title"] == "Направление сохранено"
    assert notice["change"] == "Снижение"


def test_conflict_keeps_one_reason_for_each_side():
    assessment = {"direction": "mixed", "contradictory": True, "reasons": [
        {"side": "long", "text": "Первое основание"}, {"side": "long", "text": "Второе основание"},
        {"side": "short", "text": "Основание снижения"}]}
    notice = build_notice(assessment, [], {})
    assert notice["reasons"] == ["За рост: Первое основание", "За снижение: Основание снижения"]


def test_unknown_does_not_present_unverifiable_votes_as_a_growth_signal():
    notice = build_notice({"direction": "unknown", "unknown_dependencies": ["price"],
                           "reasons": [{"side": "long", "rule_id": "LIQ_RECOVERY"}]}, [], {})
    assert "За рост" not in notice_text(notice)
    assert "Не учтено: цены" in notice_text(notice)
    assert notice["coverage"] == "Нет данных"


def test_source_text_cannot_inject_markup_or_template_fields():
    assessment = {"direction": "long", "reasons": [{"text": '</script><img src="https://evil.test"> {{stamp}}'}]}
    notice = build_notice(assessment, [], {})
    html = notification_image.notice_html(notice)
    assert '<img src="https://evil.test">' not in html
    assert "&lt;/script&gt;" in html and "{{stamp}}" in html


def test_market_keyboard_opens_frozen_assessment_and_journal_without_chart_actions():
    markup = card_keyboard(42, [{"kind": "market_context", "url": "https://example.com/events.html?assessment=a1",
                                 "journal_url": "https://example.com/?scope=context#journal"}])
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.url for b in buttons if b.url] == ["https://example.com/events.html?assessment=a1", "https://example.com/?scope=context#journal"]
    assert [b.callback_data for b in buttons if b.callback_data] == ["nf:d:42:0"]
    assert not any("График" in b.text or "Действия" in b.text for b in buttons)


async def test_image_failure_keeps_caption_details_and_correct_snapshot(db, monkeypatch):
    assessment = _asmt(direction="short", aid="frozen", evaluated=T0)
    store.insert_assessment(db, assessment)
    settings = _settings()
    publish.process_cycle(db, settings, prev=_asmt(direction="neutral"), curr=assessment, transitions=[], now_ms=T0)
    bundle = store.open_bundle(db)
    seen = []
    def fail(notice, folder):
        seen.append(notice)
        # Later history may arrive while the offline renderer is working.
        store.insert_assessment(db, _asmt(direction="long", aid="later", evaluated=T0 + 1000))
        raise RuntimeError("renderer unavailable")
    monkeypatch.setattr(publish, "_DB", db)
    monkeypatch.setattr(publish, "_SETTINGS", settings)
    monkeypatch.setattr(publish, "_now_ms", lambda: T0 + 60_000)
    monkeypatch.setattr(notification_image, "render_notice_image", fail)
    card = await publish._render_packet({"semantic_key": f"events:bundle:{bundle['id']}"}, [])
    assert seen[0]["assessment_id"] == "frozen"
    assert "перевес в сторону снижения" in card.text
    assert card.targets[0]["url"].endswith("?assessment=frozen")
    assert card.details and card.image_path is None


def test_failed_render_never_publishes_an_incomplete_png(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_image, "chromium_path", lambda: "chrome")
    monkeypatch.setattr(notification_image, "_run_chromium", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='<html data-rendered="false">'))
    with pytest.raises(RuntimeError, match="отрисовать карточку"):
        notification_image.render_notice_image(example(), tmp_path)
    assert list(tmp_path.glob("*.png")) == []


def test_long_reason_and_non_bmp_text_still_fit_photo_caption():
    assessment = {"direction": "mixed", "contradictory": True, "reasons": [
        {"side": "long", "text": "🟢 длинное основание " * 100},
        {"side": "short", "text": "🔴 другое основание " * 100}]}
    text = notice_text(build_notice(assessment, [], {}))
    assert len(text.encode("utf-16-le")) // 2 < 1000
