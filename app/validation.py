"""Source-independent validation before data enters the calculation store."""
import math
from .models import TIMEFRAME_MINUTES
from .adapters.base import PRICE_KINDS


def positive_finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: positive finite price required")


def validate_candle(candle):
    if candle.timeframe not in TIMEFRAME_MINUTES:
        raise ValueError("timeframe: unsupported")
    for name in ("open", "high", "low", "close"):
        positive_finite(getattr(candle, name), name)
    if not candle.low <= candle.open <= candle.high or not candle.low <= candle.close <= candle.high:
        raise ValueError("ohlc: low <= open/close <= high required")
    if not isinstance(candle.closed, bool):
        raise ValueError("closed: boolean required")
    if candle.open_time < 0 or candle.close_time <= candle.open_time:
        raise ValueError("candle time: invalid interval")


def accept_source_candle(candle) -> bool:
    """Граница источника: противоречивая свеча не входит в хранилище расчёта.

    Отклонение — счётчик source_candle_rejected. Хранилище само свечу не
    перепроверяет: запоздавший незакрытый снимок закрытого бара (F9) и
    внутренние записи движка проходят своим путём."""
    try:
        validate_candle(candle)
        return True
    except ValueError as exc:
        reason = str(exc).split(":", 1)[0].strip() or "invalid"
        from .services.metrics import get_registry
        get_registry().counter(
            "source_candle_rejected",
            venue=str(getattr(candle, "source", None) or "unknown"),
            reason=reason,
        )
        return False


def validate_quote(tick):
    positive_finite(tick.price, "price")
    if type(tick.received_at) is not int or (tick.event_at is not None and type(tick.event_at) is not int):
        raise ValueError("quote time: integer timestamp required")
    if tick.received_at <= 0 or (tick.event_at is not None and tick.event_at <= 0):
        raise ValueError("quote time: positive timestamp required")
    if tick.price_kind not in PRICE_KINDS:
        raise ValueError("price_kind: unsupported")
