"""§2, §8, §9: геометрия зон — глубина возврата, уровни глубины, расстояние до зоны.

Для бычьей зоны возврат ожидается сверху: глубина d = (U − price)/W,
уровень глубины d равен U − d·W. Для медвежьей — зеркально: d = (price − L)/W,
уровень L + d·W. Внутри зоны глубина в [0..1+], не дошла — отрицательная.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Protocol

from ..models import Direction, Zone


class ZoneGeometry(Protocol):
    """Минимальная геометрия зоны для точных расчётов глубины (ТЗ §4 —
    единые правила HTF/LTF): удовлетворяют и Zone, и LtfEntryZone."""

    lower: float
    upper: float
    direction: Direction


def _dec(x: float) -> Decimal:
    """Точное десятичное представление цены/границы инструмента (ТЗ §4:
    сравнения порогов без произвольного epsilon)."""
    return Decimal(str(x))


def depth_threshold_level(zone: ZoneGeometry, d: float) -> Decimal:
    """Уровень глубины d в точной арифметике: бычий P=U−d·W, медвежий P=L+d·W."""
    w = _dec(zone.upper) - _dec(zone.lower)
    dd = _dec(d)
    if zone.direction == Direction.BULL:
        return _dec(zone.upper) - dd * w
    return _dec(zone.lower) + dd * w


def reaches_depth(zone: ZoneGeometry, extreme: float, d: float) -> bool:
    """Достигнута ли глубина d экстремумом теста (точное сравнение, ТЗ §4):
    бычий OB — min(Low) <= U−d·W; медвежий — max(High) >= L+d·W.
    Ровно d считается достигнутой (для «меньше 90%» проверять строго наоборот)."""
    level = depth_threshold_level(zone, d)
    e = _dec(extreme)
    if zone.direction == Direction.BULL:
        return e <= level
    return e >= level


def exact_depth(zone: ZoneGeometry, extreme: float) -> Decimal:
    """d_raw в точной арифметике (ТЗ §4): бычий (U−extreme)/W, медвежий (extreme−L)/W."""
    w = _dec(zone.upper) - _dec(zone.lower)
    if w <= 0:
        return Decimal(0)
    if zone.direction == Direction.BULL:
        return (_dec(zone.upper) - _dec(extreme)) / w
    return (_dec(extreme) - _dec(zone.lower)) / w


def strictly_deeper(zone: Zone, extreme: float, d: float) -> bool:
    """Экстремум СТРОГО глубже уровня d (ровно d допустимо — ТЗ §6 Breaker 50%)."""
    level = depth_threshold_level(zone, d)
    e = _dec(extreme)
    if zone.direction == Direction.BULL:
        return e < level
    return e > level


def zone_depth(zone: Zone, price: float) -> float:
    """Глубина возврата цены в зону (§2). 0 — ближняя граница, 1 — дальняя."""
    w = zone.width
    if w <= 0:
        return 0.0
    if zone.direction == Direction.BULL:
        return (zone.upper - price) / w
    return (price - zone.lower) / w


def depth_level(zone: Zone, d: float) -> float:
    """Ценовой уровень глубины d (§2): U − d·W (бычья) / L + d·W (медвежья)."""
    if zone.direction == Direction.BULL:
        return zone.upper - d * zone.width
    return zone.lower + d * zone.width


def distance_to_zone(zone: Zone, price: float) -> float:
    """§9: минимальное расстояние цены до [L,U], делённое на цену; внутри — 0."""
    if price <= 0:
        return 0.0
    if zone.lower <= price <= zone.upper:
        return 0.0
    dist = zone.lower - price if price < zone.lower else price - zone.upper
    return dist / price


def in_zone(zone: Zone, price: float) -> bool:
    return zone.lower <= price <= zone.upper


def interval_intersects(zone: Zone, lo: float, hi: float) -> bool:
    """Пересекается ли наблюдаемый диапазон [lo,hi] с диапазоном зоны."""
    return lo <= zone.upper and hi >= zone.lower


def max_depth_in_interval(zone: Zone, lo: float, hi: float) -> float:
    """Максимальная глубина, достижимая диапазоном [lo,hi].

    Порядок цен внутри исторической свечи неизвестен (§11) — берём худший
    (глубочайший) reachable уровень: для бычьей зоны это lo, для медвежьей hi.
    """
    if zone.direction == Direction.BULL:
        return zone_depth(zone, lo)
    return zone_depth(zone, hi)


def approach_distance(zone: Zone, lo: float, hi: float) -> Optional[float]:
    """Расстояние до ближней границы с правильной стороны (§9, база — цена).

    None — диапазон пересекает зону или находится с неправильной стороны.
    Для бычьей зоны правильный подход — сверху, для медвежьей — снизу.
    """
    if zone.direction == Direction.BULL:
        if lo > zone.upper:
            return (lo - zone.upper) / lo
        return None
    if hi < zone.lower:
        return (zone.lower - hi) / hi
    return None


def jumped_through(zone: Zone, prev_price: Optional[float], lo: float, hi: float) -> bool:
    """§6: проход зоны насквозь между наблюдениями (тик-тик или gap между свечами).

    Цена была с ближней стороны зоны и оказалась за дальней, не побывав внутри
    наблюдаемого диапазона. Без предыдущего наблюдения скачок не доказуем
    (§13.11: отсутствие промежуточных данных отличимо от подтверждённого скачка).
    """
    if prev_price is None:
        return False
    if interval_intersects(zone, lo, hi):
        return False
    if zone.direction == Direction.BULL:
        return prev_price > zone.upper and hi < zone.lower
    return prev_price < zone.lower and lo > zone.upper


def was_near(zone: Zone, prev_price: Optional[float], approach_pct: float) -> bool:
    """Была ли предыдущая цена в зоне или в полосе приближения (для подавления
    повторного APPROACH внутри одного эпизода подхода)."""
    if prev_price is None:
        return False
    if in_zone(zone, prev_price):
        return True
    if zone.direction == Direction.BULL:
        return zone.upper < prev_price <= zone.upper * (1 + approach_pct)
    return zone.lower * (1 - approach_pct) <= prev_price < zone.lower
