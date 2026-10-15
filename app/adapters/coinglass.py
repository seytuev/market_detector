"""Клиент Coinglass API v4. Ключ не попадает в текст ошибок и логи."""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal


class CoinglassError(Exception):
    def __init__(self, message: str, *, code: str | None = None, http: int | None = None):
        super().__init__(message)
        self.code = code
        self.http = http


class CapabilityError(CoinglassError):
    pass


class AuthError(CoinglassError):
    pass


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text


def interpret_body(http: int, body: dict) -> dict:
    """Успех — прикладной code "0". HTTP 200 с code 403 — отказ тарифа, не данные."""
    code = body.get("code")
    if code in ("0", 0):
        return body
    msg = str(body.get("msg") or body.get("message") or "ошибка Coinglass")
    if str(code) in {"401", "40100"} or http == 401:
        raise AuthError(msg, code=str(code), http=http)
    if str(code) == "403" or "not available for your current API plan" in msg:
        raise CapabilityError(msg, code=str(code), http=http)
    if http == 429 or str(code) == "429":
        raise CoinglassError(msg, code="429", http=http or 429)
    raise CoinglassError(msg, code=None if code is None else str(code), http=http)


def as_decimal(value) -> Decimal:
    return Decimal(str(value))


class CoinglassClient:
    def __init__(self, api_key: str, base_url: str = "https://open-api-v4.coinglass.com",
                 timeout: float = 20.0, *, cache_seconds: float = 0):
        if not api_key:
            raise AuthError("ключ Coinglass не задан")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self.last_limits: dict[str, str] = {}
        self._cache_seconds = max(0, cache_seconds)
        self._cache = OrderedDict()
        self._cache_lock = RLock()

    def __repr__(self) -> str:
        return "CoinglassClient(key=set)"

    def get(self, path: str, params: dict | None = None) -> dict:
        if not self._cache_seconds:
            return self._get(path, params)
        key = (path, urllib.parse.urlencode(sorted((params or {}).items())))
        # One account client is shared across cycles and manual refreshes.
        # Serialize misses so concurrent callers cannot multiply source traffic.
        with self._cache_lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(key)
                return deepcopy(cached[1])
            body = self._get(path, params)
            # Closed 4h/daily windows must be fetched again at their boundary,
            # even if the short reuse period has not elapsed. Failures are never
            # cached and an expired successful response never hides a failure.
            until_boundary = 14_400 - time.time() % 14_400
            expires = time.monotonic() + min(self._cache_seconds, until_boundary)
            self._cache[key] = (expires, deepcopy(body))
            self._cache.move_to_end(key)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
            return body

    def _get(self, path: str, params: dict | None = None) -> dict:
        query = urllib.parse.urlencode(params or {})
        url = self._base + path + (("?" + query) if query else "")
        req = urllib.request.Request(
            url,
            headers={"CG-API-KEY": self._key, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                http = resp.status
                self._read_limits(resp.headers)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            http = exc.code
            self._read_limits(exc.headers)
            raw = exc.read()
        except Exception as exc:
            raise CoinglassError(redact(str(exc), self._key)) from None
        text = redact(raw.decode("utf-8", "replace"), self._key)
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CoinglassError("ответ Coinglass не JSON") from exc
        if not isinstance(body, dict):
            raise CoinglassError("ответ Coinglass не объект")
        return interpret_body(http, body)

    def _read_limits(self, headers) -> None:
        if not headers:
            return
        for key, value in headers.items():
            low = key.lower()
            if low in {"api-key-max-limit", "api-key-use-limit", "retry-after"}:
                self.last_limits[low] = value

    def liquidations(self, symbol: str, exchange_list: str, interval: str = "1d",
                     limit: int = 150) -> list[dict]:
        body = self.get(
            "/api/futures/liquidation/aggregated-history",
            {
                "exchange_list": exchange_list,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        return list(body.get("data") or [])

    def open_interest(self, symbol: str, unit: str, interval: str = "1d",
                      limit: int = 150) -> list[dict]:
        if unit not in {"usd", "coin"}:
            raise CoinglassError("unit должен быть usd или coin")
        body = self.get(
            "/api/futures/open-interest/aggregated-history",
            {"symbol": symbol, "interval": interval, "limit": limit, "unit": unit},
        )
        return list(body.get("data") or [])

    def funding(self, exchange: str, symbol: str, interval: str = "1d",
                limit: int = 150) -> list[dict]:
        body = self.get(
            "/api/futures/funding-rate/history",
            {
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        return list(body.get("data") or [])

    def subscription(self) -> dict:
        body = self.get("/api/user/account/subscription")
        return dict(body.get("data") or {})
