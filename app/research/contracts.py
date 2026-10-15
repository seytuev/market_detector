"""Small, explicit integration contract; published time never grants availability."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import uuid
import threading
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from ..models import now_ms

VERSION = "research-integration-1"
KINDS = {"assets", "materials", "analyses", "catalyst_candidates", "summaries"}
_id_lock = threading.Lock()
_id_time = 0


class ResearchError(Exception):
    def __init__(self, code, message, status=422, fields=None):
        super().__init__(message)
        self.code, self.message, self.status, self.fields = code, message, status, fields or {}


def uid():
    # Lexical ordering breaks equal-millisecond ties in immutable local revisions.
    global _id_time
    with _id_lock:
        _id_time = max(time.time_ns() // 1000, _id_time + 1)
        return str(uuid.UUID(int=(_id_time << 64) | (uuid.uuid4().int & ((1 << 64)-1))))


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode(value, fallback=None):
    if value is None:
        return fallback
    return json.loads(value)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def text(value, name, minimum=0, maximum=10000):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ResearchError("invalid_field", f"Проверьте поле «{name}»", fields={name: "Недопустимая длина или тип"})
    return value.strip()


def choice(value, allowed, name):
    if value not in allowed:
        raise ResearchError("invalid_field", f"Недопустимое значение «{name}»", fields={name: sorted(allowed)})
    return value


def timestamp(value, name, *, optional=True, future=True):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 4_102_444_800_000:
        raise ResearchError("invalid_time", f"Проверьте время «{name}»")
    if not future and value > now_ms() + 60_000:
        raise ResearchError("future_availability", "Время получения находится в будущем")
    return value


def finite(value, name):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
        raise ResearchError("invalid_number", f"Проверьте число «{name}»")
    return value


def safe_url(value):
    if not value:
        return ""
    try:
        url = urlsplit(text(value, "url", maximum=2048))
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            raise ValueError()
        query = [(k, v) for k, v in parse_qsl(url.query) if not re.match(r"^(utm_|ref$|s$|t$)", k)]
        if any(re.search(r"token|secret|password|api.?key|signature", k, re.I) for k, _ in query):
            raise ValueError()
        return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
    except (ValueError, ResearchError):
        raise ResearchError("invalid_url", "Нужна публичная HTTP(S)-ссылка без секретов")


def cursor(value):
    return base64.urlsafe_b64encode(encode(value).encode()).decode().rstrip("=")


def uncursor(value):
    if not value:
        return None
    try:
        if len(value) > 4096:
            raise ValueError()
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, TypeError, UnicodeError):
        raise ResearchError("invalid_cursor", "Некорректный курсор", 400)


def quote_supported(quote, original):
    norm = lambda s: re.sub(r"\s+", " ", str(s)).strip()
    return bool(norm(quote)) and norm(quote) in norm(original)


def validate_item(raw):
    if not isinstance(raw, dict):
        raise ResearchError("invalid_item", "Некорректная запись экспорта")
    item = dict(raw)
    choice(item.get("entity_kind"), KINDS, "entity_kind")
    for field in ("entity_id", "revision_id"):
        item[field] = text(item.get(field), field, 1, 240)
    choice(item.get("operation"), {"upsert", "tombstone"}, "operation")
    seq = item.get("change_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ResearchError("invalid_sequence", "Некорректная последовательность изменений")
    if not isinstance(item.get("payload"), dict) or len(encode(item["payload"]).encode()) > 200_000:
        raise ResearchError("invalid_payload", "Некорректный или слишком большой объект")
    if item.get("payload_hash") != digest(item["payload"]):
        raise ResearchError("hash_mismatch", "Контрольная сумма экспорта не совпадает")
    return item
