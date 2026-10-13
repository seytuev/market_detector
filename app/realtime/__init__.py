"""Realtime-подписки на рыночные данные (этап 3, спека §7).

Binance Spot WebSocket — за флагом HTF_REALTIME_ENABLED (по умолчанию
выключено, поведение идентично REST-опросу, AC21).
"""
from .binance_ws import BinanceRealtime

__all__ = ["BinanceRealtime"]
