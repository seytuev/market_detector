"""Адаптер CoinMarketCap Pro API (модуль «Altcoins D1 accumulation»).

Единственный метод — listings_latest: снапшот рейтинга по market cap
(GET /v1/cryptocurrency/listings/latest?start=1&limit=N&convert=USD).
Ключ передаётся заголовком X-CMC_PRO_API_KEY.

Ошибки — явные AdapterError, без фолбэков (§1): пустой ключ (на вызове,
конструктор не падает), HTTP-ошибка, битый payload. Пустую вселенную
адаптер не выдумывает — это решение refresh_universe (app/alt/universe.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

from .base import AdapterError


@dataclass
class CmcListing:
    """Строка рейтинга CMC. cmc_id — стабильная идентичность (symbol — нет)."""
    cmc_id: int
    name: str
    symbol: str
    rank: int                      # cmc_rank
    market_cap: float
    tags: list[str] = field(default_factory=list)
    date_added: str = ""
    platform_info: Optional[dict] = None   # null или объект platform (токен на чужой сети)


class CoinMarketCapAdapter:
    """Клиент CoinMarketCap Pro API. venue='coinmarketcap'."""

    venue = "coinmarketcap"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: Optional[httpx.AsyncClient] = None,
    ):
        # Пустой ключ здесь НЕ ошибка: конструкция не должна падать,
        # ошибка всплывёт на первом вызове listings_latest.
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            base_url=self._base,
            timeout=30.0,
            headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"},
        )
        self._own_client = client is None

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def listings_latest(self, limit: int = 300) -> list[CmcListing]:
        """Снапшот топ-N по market cap (start=1, convert=USD)."""
        if not self._api_key:
            raise AdapterError("CMC_API_KEY not configured")
        try:
            resp = await self._client.get(
                "/v1/cryptocurrency/listings/latest",
                params={"start": 1, "limit": limit, "convert": "USD"},
            )
        except httpx.HTTPError as e:
            raise AdapterError(f"cmc: сеть/таймаут при listings/latest: {e}") from e
        if resp.status_code != 200:
            msg = ""
            try:
                status = resp.json().get("status") or {}
                msg = status.get("error_message") or ""
            except ValueError:
                msg = resp.text[:200]
            raise AdapterError(
                f"cmc: HTTP {resp.status_code} при listings/latest: {msg}"
            )
        try:
            data = resp.json()["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise AdapterError(f"cmc: неожиданный формат ответа: {e}") from e
        if not isinstance(data, list):
            raise AdapterError("cmc: поле data не список")
        return [_parse_listing(row) for row in data]


def _parse_listing(row: dict) -> CmcListing:
    try:
        quote_usd = (row.get("quote") or {}).get("USD") or {}
        tags = row.get("tags") or []
        return CmcListing(
            cmc_id=int(row["id"]),
            name=str(row.get("name") or ""),
            symbol=str(row.get("symbol") or ""),
            rank=int(row["cmc_rank"]),
            market_cap=float(quote_usd.get("market_cap") or 0.0),
            tags=[str(t) for t in tags],
            date_added=str(row.get("date_added") or ""),
            platform_info=row.get("platform"),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise AdapterError(
            f"cmc: битая строка листинга (id={row.get('id')}): {e}"
        ) from e
