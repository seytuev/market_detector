"""P0-фиксы: строгий парсинг bool из ENV/JSON (bool("false") == True — ловушка)
и публичный базовый URL для ссылок из Telegram."""
from __future__ import annotations

import pytest

from app.config import DetectorConfig, Settings, load_detector_config, parse_bool
from app.web.api import _apply_detector_payload


def test_parse_bool_variants():
    for raw in ("true", "True", "TRUE", "1", "yes", "on", True, 1):
        assert parse_bool(raw) is True
    for raw in ("false", "False", "FALSE", "0", "no", "off", False, 0):
        assert parse_bool(raw) is False


def test_parse_bool_invalid():
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_env_bool_false_stays_false(monkeypatch):
    """HTF_DET_NOTIFY_ONLY_REVIEWED=false не должен ВКЛЮЧАТЬ режим."""
    monkeypatch.setenv("HTF_DET_NOTIFY_ONLY_REVIEWED", "false")
    assert load_detector_config().notify_only_reviewed is False


def test_env_bool_true(monkeypatch):
    monkeypatch.setenv("HTF_DET_NOTIFY_ONLY_REVIEWED", "true")
    assert load_detector_config().notify_only_reviewed is True


def test_env_bool_invalid_ignored(monkeypatch):
    monkeypatch.setenv("HTF_DET_NOTIFY_ONLY_REVIEWED", "junk")
    assert load_detector_config().notify_only_reviewed is False


def test_apply_payload_bool_string():
    """Строка "false" из настроек не превращается в True."""
    cfg = DetectorConfig()
    applied = _apply_detector_payload(cfg, {"notify_only_reviewed": "false"})
    assert cfg.notify_only_reviewed is False
    assert applied == ["notify_only_reviewed"]


def test_apply_payload_bool_native():
    cfg = DetectorConfig(notify_only_reviewed=True)
    _apply_detector_payload(cfg, {"notify_only_reviewed": False})
    assert cfg.notify_only_reviewed is False


def test_effective_base_url_wildcard_host():
    """0.0.0.0 слушает сокет, но в ссылке бессмысленен → 127.0.0.1."""
    s = Settings(host="0.0.0.0", port=8000)
    assert s.effective_base_url() == "http://127.0.0.1:8000"


def test_effective_base_url_public_override():
    s = Settings(public_base_url="https://htf.example.com/")
    assert s.effective_base_url() == "https://htf.example.com"


def test_lookback_days_for_defaults():
    """§1: раздельная глубина истории — D1 год, W1 два года, прочие — fallback."""
    cfg = DetectorConfig()
    assert cfg.lookback_days_for("D1") == 365
    assert cfg.lookback_days_for("W1") == 730
    assert cfg.lookback_days_for("H1") == cfg.lookback_days


def test_lookback_days_for_env_override(monkeypatch):
    monkeypatch.setenv("HTF_DET_LOOKBACK_DAYS_W1", "3650")
    cfg = load_detector_config()
    assert cfg.lookback_days_for("W1") == 3650
    assert cfg.lookback_days_for("D1") == 365
