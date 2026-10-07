"""Вселенная альткоинов модуля «Altcoins D1 accumulation».

Дневной снапшот рейтинга CoinMarketCap по market cap → базовая выборка
`cmc_rank_min <= cmc_rank <= cmc_rank_max` (по умолчанию 11..300) минус
исключения: стейблкоины, wrapped-токены, дубликаты/деривативы топ-10.

Принципы (TZ):
- cmc_id — стабильная идентичность, symbol — НЕТ: совпадение тикера у
  разных cmc_id без канонического маппинга → mapping_status="mapping_pending",
  тихого слияния двух монет с одним тикером никогда не бывает;
- НЕТ добора из рангов 301+ после исключений — выборка может быть < 290;
- при ошибке CMC — последний успешный снапшот с флагом stale, список
  не опустошается; если подтверждённого снапшота нет вообще — анализ
  недоступен (DATA_PENDING), топ-300 не выдумывается.

Правила исключений — явные, data-driven (редактируемый конфиг ниже):
- STABLE_SYMBOLS / тег "stablecoin" → "stable";
- тег "wrapped" / WRAPPED_SYMBOLS / CANONICAL_MAP(kind="wrapped") → "wrapped";
- тег "binance-peg" / CANONICAL_MAP(kind="duplicate") → "duplicate".

Ручные правки (convention): если у существующей строки alt_asset
mapping_status == "manual", refresh сохраняет её exclusion_category,
mapping_status, mapping_reason и enabled — обновляются только
symbol/name/cmc_rank. Это канал ручного разбора mapping_pending.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from ..adapters.base import AdapterError
from ..adapters.coinmarketcap import CmcListing
from ..config import AltConfig
from ..db import Database
from ..models_alt import AltAsset

# --- Редактируемые правила исключений (data-driven конфиг) ---

# Стейблкоины: тег CMC "stablecoin" ловит большинство; символы — страховка
# для случаев, когда тега нет или он нестандартный.
STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "PYUSD",
    "BUSD", "USDP", "GUSD", "LUSD", "FRAX", "USD1", "EURS", "EURT",
}

# Wrapped: в первую очередь тег CMC "wrapped"; символы — страховка для
# обёрток без тега (префикс "w" у известных обёрток топ-активов).
WRAPPED_TAGS = {"wrapped"}
WRAPPED_SYMBOLS = {"WBTC", "WETH", "WBNB", "WSTETH", "WEETH", "CBBTC"}

# Каноническая карта известных обёрток/деривативов: cmc_id → экономический
# (канонический) актив. kind: "wrapped" — обёртка, "duplicate" — пег/дериватив
# канонического актива топ-10 (оба исключаются); любой другой kind
# (например "alias" — ребрендинг/вторая запись того же актива) НЕ исключает,
# а только связывает canonical_asset_id для дедупликации в select_universe.
# Один канонический asset на экономический актив.
CANONICAL_MAP: dict[int, dict] = {
    3717: {"canonical_asset_id": "BTC", "kind": "wrapped", "note": "Wrapped BTC"},
    23694: {"canonical_asset_id": "BTC", "kind": "wrapped", "note": "Coinbase Wrapped BTC"},
    2396: {"canonical_asset_id": "ETH", "kind": "wrapped", "note": "WETH"},
    8085: {"canonical_asset_id": "ETH", "kind": "duplicate", "note": "Lido stETH — дериватив ETH"},
    29242: {"canonical_asset_id": "ETH", "kind": "wrapped", "note": "Wrapped eETH"},
    7192: {"canonical_asset_id": "BNB", "kind": "wrapped", "note": "WBNB"},
    23095: {"canonical_asset_id": "SOL", "kind": "duplicate", "note": "Binance-Peg SOL"},
}

EXC_STABLE = "stable"
EXC_WRAPPED = "wrapped"
EXC_DUPLICATE = "duplicate"

MAPPING_PENDING_REASON = "same symbol, different CMC id"


def classify_exclusion(listing: CmcListing) -> Optional[str]:
    """Категория исключения по тегам/метаданным CMC или None (включаем).

    Порядок: stable → wrapped → duplicate (первое срабатывание решает).
    """
    tags = {t.lower() for t in listing.tags}
    symbol = listing.symbol.upper()
    canonical = CANONICAL_MAP.get(listing.cmc_id)

    if any("stablecoin" in t for t in tags) or symbol in STABLE_SYMBOLS:
        return EXC_STABLE
    if (
        tags & WRAPPED_TAGS
        or symbol in WRAPPED_SYMBOLS
        or (canonical and canonical["kind"] == "wrapped")
    ):
        return EXC_WRAPPED
    if any("binance-peg" in t for t in tags) or (
        canonical and canonical["kind"] == "duplicate"
    ):
        return EXC_DUPLICATE
    return None


def _to_asset(listing: CmcListing, exclusion: Optional[str]) -> AltAsset:
    canonical = CANONICAL_MAP.get(listing.cmc_id)
    asset = AltAsset(
        id=None,
        cmc_id=listing.cmc_id,
        symbol=listing.symbol,
        name=listing.name,
        cmc_rank=listing.rank,
        exclusion_category=exclusion,
        canonical_asset_id=canonical["canonical_asset_id"] if canonical else None,
    )
    if canonical:
        asset.mapping_status = "mapped"
        asset.mapping_reason = canonical["note"]
    if exclusion:
        asset.mapping_reason = (
            f"{asset.mapping_reason}; excluded: {exclusion}".lstrip("; ")
        )
    return asset


def select_universe(
    listings: list[CmcListing], cfg: AltConfig
) -> tuple[list[AltAsset], list[AltAsset]]:
    """(included, excluded-with-reason) по снапшоту CMC.

    - окно рангов [cfg.cmc_rank_min, cfg.cmc_rank_max]; вне окна — не
      отслеживаем вообще (добора из 301+ НЕТ, выборка может быть меньше
      окна после исключений);
    - исключения classify_exclusion уходят во второй список с категорией;
    - дубликат canonical_asset_id внутри выборки: остаётся меньший ранг,
      остальные — excluded с категорией "duplicate";
    - один symbol у разных cmc_id без канонического маппинга → оба
      mapping_status="mapping_pending" (не сливаем), остаются в included.
    """
    included: list[AltAsset] = []
    excluded: list[AltAsset] = []

    for listing in listings:
        if not (cfg.cmc_rank_min <= listing.rank <= cfg.cmc_rank_max):
            continue  # вне окна — не трек-аем (никакого бэкфилла из 301+)
        exclusion = classify_exclusion(listing)
        asset = _to_asset(listing, exclusion)
        (excluded if exclusion else included).append(asset)

    # Дубликаты канонического актива: оставляем лучший (меньший) ранг.
    by_canonical: dict[str, list[AltAsset]] = {}
    for asset in included:
        if asset.canonical_asset_id:
            by_canonical.setdefault(asset.canonical_asset_id, []).append(asset)
    for group in by_canonical.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda a: a.cmc_rank)
        for dup in group[1:]:
            dup.exclusion_category = EXC_DUPLICATE
            dup.mapping_reason = (
                f"{dup.mapping_reason}; duplicate canonical asset "
                f"{dup.canonical_asset_id}, kept cmc_id={group[0].cmc_id}".lstrip("; ")
            )
            included.remove(dup)
            excluded.append(dup)

    # Один тикер у разных cmc_id без канонического маппинга — не сливаем.
    by_symbol: dict[str, list[AltAsset]] = {}
    for asset in included:
        by_symbol.setdefault(asset.symbol.upper(), []).append(asset)
    for group in by_symbol.values():
        if len({a.cmc_id for a in group}) < 2:
            continue
        for asset in group:
            if asset.canonical_asset_id:
                continue  # каноническая карта разруливает идентичность
            asset.mapping_status = "mapping_pending"
            asset.mapping_reason = MAPPING_PENDING_REASON

    included.sort(key=lambda a: a.cmc_rank)
    excluded.sort(key=lambda a: a.cmc_rank)
    return included, excluded


def _listing_from_dict(d: dict) -> CmcListing:
    return CmcListing(
        cmc_id=int(d["cmc_id"]),
        name=str(d.get("name") or ""),
        symbol=str(d.get("symbol") or ""),
        rank=int(d.get("rank", d.get("cmc_rank", 0))),
        market_cap=float(d.get("market_cap") or 0.0),
        tags=[str(t) for t in (d.get("tags") or [])],
        date_added=str(d.get("date_added") or ""),
        platform_info=d.get("platform_info"),
    )


async def refresh_universe(
    db: Database, adapter, cfg: AltConfig, now_ms: int
) -> dict:
    """Дневное обновление вселенной: снапшот CMC → alt_universe_snapshot + alt_asset.

    Успех: новый снапшот (payload_json = сырые строки рейтинга) и upsert
    активов; ручные правки (mapping_status="manual") сохраняются.
    Ошибка CMC: последний успешный снапшот с stale=True, активы не
    переписываются; снапшота нет вообще — {"stale": True, "empty": True}
    (анализ недоступен, DATA_PENDING — топ-300 не выдумываем).
    Активы, выпавшие из выборки, остаются в alt_asset с прежним enabled —
    «вне текущей вселенной» разруливает движок, здесь только возвращаем
    текущий набор (included_cmc_ids).
    """
    try:
        listings = await adapter.listings_latest(limit=cfg.cmc_listing_limit)
    except AdapterError as e:
        snap = db.get_latest_alt_universe_snapshot()
        if snap is None:
            return {
                "stale": True, "empty": True, "error": str(e),
                "snapshot_id": None, "included": 0, "excluded": 0,
                "included_cmc_ids": [],
            }
        stale_listings = [_listing_from_dict(d) for d in json.loads(snap["payload_json"])]
        included, excluded = select_universe(stale_listings, cfg)
        return {
            "stale": True, "empty": False, "error": str(e),
            "snapshot_id": int(snap["id"]),
            "snapshot_taken_ms": int(snap["taken_ms"]),
            "included": len(included), "excluded": len(excluded),
            "included_cmc_ids": [a.cmc_id for a in included],
        }

    included, excluded = select_universe(listings, cfg)
    snapshot_id = db.insert_alt_universe_snapshot(
        taken_ms=now_ms,
        payload_json=json.dumps([asdict(l) for l in listings], ensure_ascii=False),
        source="cmc",
        stale=False,
    )
    for asset in [*included, *excluded]:
        existing = db.get_alt_asset_by_cmc_id(asset.cmc_id)
        if existing is not None:
            asset.id = existing.id
            asset.created_ms = existing.created_ms
            if existing.mapping_status == "manual":
                # Ручной разбор: сохраняем вердикт, обновляем только справочные поля.
                asset.exclusion_category = existing.exclusion_category
                asset.mapping_status = existing.mapping_status
                asset.mapping_reason = existing.mapping_reason
                asset.enabled = existing.enabled
        asset.updated_ms = now_ms
        db.upsert_alt_asset(asset)

    return {
        "stale": False, "empty": False, "error": None,
        "snapshot_id": snapshot_id,
        "snapshot_taken_ms": now_ms,
        "listings_total": len(listings),
        "included": len(included), "excluded": len(excluded),
        "mapping_pending": sum(
            1 for a in included if a.mapping_status == "mapping_pending"
        ),
        "included_cmc_ids": [a.cmc_id for a in included],
    }
