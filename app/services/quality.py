"""F03/A03: единая серверная оценка качества данных инструмента.

Раньше решение «данных достаточно» принималось в четырёх местах по-разному
(_data_state, service_status, instrument_current, гейт доставки LTF) — с
разными единицами сравнения (курсор обработки хранит close_time, а
сравнивался с open_time). Теперь — одна функция data_quality с
поканальной детализацией; все потребители используют её контракт.

Каналы: котировка, закрытые свечи H1/D1/W1, непрерывность истории H1,
курсор обработки движка (meta ltf:h1:last_close — close_time последней
обработанной H1), флаг восстановления (meta replaying:). Сводный state
сохраняет прежний словарь: replaying → data_pending → stale → ok; D1/W1 —
совещательные (сами по себе сводку в stale не переводят, основной канал
свежести — H1).
"""
from __future__ import annotations

from typing import Any, Optional

from ..db import Database
from ..models import TIMEFRAME_MINUTES

H1_MS = TIMEFRAME_MINUTES["H1"] * 60_000

# сколько последних закрытых H1 сканируем на разрывы истории
_CONTINUITY_SCAN = 50


def _channel(
    status: str,
    last_at: Optional[int] = None,
    age_s: Optional[float] = None,
    threshold_s: Optional[float] = None,
    reason: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    ch: dict[str, Any] = {
        "status": status,          # ok | stale | missing | lagging | inactive
        "last_at": last_at,
        "age_s": age_s,
        "threshold_s": threshold_s,
        "reason": reason,
    }
    ch.update(extra)
    return ch


def quote_stale_limit_ms(settings) -> int:
    """Порог устаревания котировки (мс). Явная настройка stale_quote_seconds
    важнее; авто-порог — от интервала опроса котировок: быстрый цикл F02
    (quote_poll_seconds > 0) или HTF-цикл, когда он выключен."""
    det = settings.detector
    if det.stale_quote_seconds > 0:
        return det.stale_quote_seconds * 1000
    quote_seconds = getattr(settings, "quote_poll_seconds", 0)
    if quote_seconds > 0:
        return max(2 * quote_seconds, 30) * 1000
    return 2 * settings.poll_seconds * 1000


def _quote_channel(db: Database, settings, instrument_id: int,
                   now: int) -> dict[str, Any]:
    limit_ms = quote_stale_limit_ms(settings)
    quote = db.get_quote(instrument_id)
    if quote is None:
        return _channel("missing", threshold_s=limit_ms / 1000,
                        reason="no_quote")
    age_s = round((now - quote[1]) / 1000, 1)
    if now - quote[1] > limit_ms:
        return _channel("stale", last_at=quote[1], age_s=age_s,
                        threshold_s=limit_ms / 1000, reason="quote_stale")
    return _channel("ok", last_at=quote[1], age_s=age_s,
                    threshold_s=limit_ms / 1000)


def _tf_channel(db: Database, settings, instrument_id: int,
                tf: str, now: int) -> dict[str, Any]:
    """Свежесть последней закрытой свечи ТФ: возраст close_time против
    stale_<tf>_intervals интервалов."""
    det = settings.detector
    intervals = {
        "H1": det.stale_h1_intervals,
        "D1": det.stale_d1_intervals,
        "W1": det.stale_w1_intervals,
    }[tf]
    tf_ms = TIMEFRAME_MINUTES[tf] * 60_000
    limit_ms = intervals * tf_ms
    last = db.last_candle(instrument_id, tf)
    if last is None:
        return _channel("missing", threshold_s=limit_ms / 1000,
                        reason=f"no_{tf.lower()}_candles")
    age_s = round((now - last.close_time) / 1000, 1)
    if now - last.close_time > limit_ms:
        return _channel("stale", last_at=last.close_time, age_s=age_s,
                        threshold_s=limit_ms / 1000,
                        reason=f"{tf.lower()}_stale")
    return _channel("ok", last_at=last.close_time, age_s=age_s,
                    threshold_s=limit_ms / 1000)


def _continuity_channel(db: Database, instrument_id: int) -> dict[str, Any]:
    """Разрывы истории: промежуток между close_time соседних закрытых H1
    больше одного интервала. Меньше двух свечей — проверять нечего."""
    candles = db.get_candles(instrument_id, "H1")[-_CONTINUITY_SCAN:]
    gaps = sum(
        1 for a, b in zip(candles, candles[1:])
        if b.close_time - a.close_time > H1_MS
    )
    if gaps:
        return _channel("stale", reason="history_gap", gaps=gaps)
    return _channel("ok", gaps=0)


def _processing_channel(db: Database, instrument_id: int) -> dict[str, Any]:
    """Отставание расчёта: курсор ltf:h1:last_close (close_time последней
    обработанной H1) против close_time последней закрытой — одни единицы
    (раньше сравнивался с open_time — ложное отставание, A03)."""
    last = db.last_candle(instrument_id, "H1")
    if last is None:
        return _channel("ok")  # нет свечей — отставать не от чего
    raw = db.get_meta(f"ltf:h1:last_close:{instrument_id}")
    cursor = int(raw) if raw else None
    if cursor is None or cursor < last.close_time:
        return _channel("lagging", last_at=cursor,
                        reason="processing_lag")
    return _channel("ok", last_at=cursor)


def _replay_channel(db: Database, instrument_id: int) -> dict[str, Any]:
    if db.get_meta(f"replaying:{instrument_id}") == "1":
        return _channel("stale", reason="replay_in_progress")
    return _channel("ok")


def data_quality(db: Database, settings, instrument_id: int,
                 now_ms: int) -> dict[str, Any]:
    """Качество данных инструмента на момент now_ms.

    Возвращает сводные state/reason (прежний словарь _data_state), карту
    каналов и плоские legacy-поля для обратной совместимости ответа
    data_state (только аддитивно)."""
    channels = {
        "quote": _quote_channel(db, settings, instrument_id, now_ms),
        "h1": _tf_channel(db, settings, instrument_id, "H1", now_ms),
        "d1": _tf_channel(db, settings, instrument_id, "D1", now_ms),
        "w1": _tf_channel(db, settings, instrument_id, "W1", now_ms),
        "continuity": _continuity_channel(db, instrument_id),
        "processing": _processing_channel(db, instrument_id),
        "replay": _replay_channel(db, instrument_id),
    }
    # legacy-поля — те же значения, что отдавал _data_state
    source_stale = db.get_meta(f"stale:{instrument_id}:H1") == "1"
    out: dict[str, Any] = {
        "quote_at": channels["quote"]["last_at"],
        "quote_age_s": channels["quote"]["age_s"],
        "quote_stale": channels["quote"]["status"] == "stale",
        "h1_last_close": channels["h1"]["last_at"],
        "h1_age_s": channels["h1"]["age_s"],
        "h1_stale": channels["h1"]["status"] == "stale",
        "source_stale": source_stale,
        "channels": channels,
    }
    # сводка: прежние приоритеты; новые причины — processing_lag (расчёт
    # отстаёт) и history_gap (разрыв истории). D1/W1 сводку не меняют.
    if channels["replay"]["status"] != "ok":
        return {"state": "replaying", "reason": "replay_in_progress", **out}
    if channels["h1"]["status"] == "missing":
        return {"state": "data_pending", "reason": "no_h1_candles", **out}
    if channels["quote"]["status"] == "missing":
        return {"state": "data_pending", "reason": "no_quote", **out}
    if channels["quote"]["status"] == "stale":
        return {"state": "stale", "reason": "quote_stale", **out}
    if channels["h1"]["status"] == "stale":
        return {"state": "stale", "reason": "h1_stale", **out}
    if source_stale:
        return {"state": "stale", "reason": "source_stale", **out}
    if channels["processing"]["status"] == "lagging":
        return {"state": "stale", "reason": "processing_lag", **out}
    if channels["continuity"]["status"] == "stale":
        return {"state": "stale", "reason": "history_gap", **out}
    return {"state": "ok", "reason": None, **out}
