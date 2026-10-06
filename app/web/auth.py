"""Приватный доступ владельца (§11 п.8 спеки).

Проверка токена: заголовок ``Authorization: Bearer <token>`` или query ``?token=``.
WebSocket — только через query (браузер не шлёт произвольные заголовки).
Секреты (токены бота, auth_token) никогда не отдаются наружу через API.
"""
from __future__ import annotations

import hmac
from typing import Optional
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Query, Request, WebSocket

from ..config import Settings

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_host(origin: str) -> Optional[str]:
    try:
        return urlparse(origin).netloc.lower() or None
    except ValueError:
        return None


def _origin_allowed(origin: Optional[str], host: Optional[str]) -> bool:
    """Проверка Origin (§7.6): заголовок отсутствует (небраузерный клиент) —
    допустимо; присутствует — обязан совпадать с Host запроса, иначе это
    чужой источник (DNS-rebinding/межсайтовый вызов)."""
    if not origin:
        return True
    oh = _origin_host(origin)
    return oh is not None and host is not None and oh == host.lower()


def _extract_token(authorization: Optional[str], query_token: Optional[str]) -> Optional[str]:
    """Достаёт токен из Bearer-заголовка, иначе из query-параметра."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return query_token


def _token_ok(candidate: Optional[str], expected: str) -> bool:
    """Сравнение в constant-time: по разнице ответа нельзя подобрать токен."""
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def make_auth_dependency(settings: Settings):
    """Dependency для HTTP-роутов: 401 без валидного токена."""

    async def require_auth(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> None:
        candidate = _extract_token(authorization, token)
        if not _token_ok(candidate, settings.auth_token):
            raise HTTPException(status_code=401, detail="Требуется токен владельца")
        # §7.6: изменяющие запросы — только со своего Origin
        if request.method in _MUTATING_METHODS and not _origin_allowed(
            request.headers.get("origin"), request.headers.get("host")
        ):
            raise HTTPException(status_code=403, detail="Чужой Origin")

    return require_auth


async def check_ws_token(websocket: WebSocket, settings: Settings) -> bool:
    """Проверка токена и Origin WebSocket (§7.6; токен — query ``?token=``).
    False — соединение закрыто."""
    candidate = websocket.query_params.get("token")
    if not _token_ok(candidate, settings.auth_token):
        await websocket.close(code=4401)
        return False
    if not _origin_allowed(
        websocket.headers.get("origin"), websocket.headers.get("host")
    ):
        await websocket.close(code=4403)
        return False
    return True
