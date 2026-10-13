"""Приватный доступ владельца (§11 п.8 спеки).

Проверка токена: заголовок ``Authorization: Bearer <token>`` или query ``?token=``.
WebSocket — только через query (браузер не шлёт произвольные заголовки).
Секреты (токены бота, auth_token) никогда не отдаются наружу через API.
"""
from __future__ import annotations

import hmac
import hashlib
import secrets
import asyncio
from ..models import now_ms
from typing import Optional
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Query, Request, WebSocket, Body, Response, Depends

from ..config import Settings

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
session_cookie_name = "lf_session"


def _session_ok(db, cookie, settings):
    if db is None or not cookie or len(cookie) > 200:
        return False
    digest = hashlib.sha256(cookie.encode()).hexdigest()
    row = db.conn.execute("SELECT * FROM browser_session WHERE token_hash=?", (digest,)).fetchone()
    return bool(row and row["expires_at"] > now_ms() and row["owner_hash"] == hashlib.sha256(settings.auth_token.encode()).hexdigest())


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
    return bool(candidate) and hmac.compare_digest(candidate.encode(), expected.encode())


def make_auth_dependency(settings: Settings, db=None):
    """Dependency для HTTP-роутов: 401 без валидного токена."""

    async def require_auth(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> None:
        candidate = _extract_token(authorization, token)
        if not _token_ok(candidate, settings.auth_token) and not _session_ok(db, request.cookies.get(session_cookie_name), settings):
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
    db = getattr(websocket.app.state, "db", None)
    candidate = _extract_token(
        websocket.headers.get("authorization"),
        websocket.query_params.get("token"),
    )
    if not _token_ok(candidate, settings.auth_token) and not _session_ok(db, websocket.cookies.get(session_cookie_name), settings):
        await websocket.close(code=4401)
        return False
    if not _origin_allowed(
        websocket.headers.get("origin"), websocket.headers.get("host")
    ):
        await websocket.close(code=4403)
        return False
    return True


def register_auth_routes(app, db, settings, require_auth):
    @app.post("/api/auth/session")
    async def login(request: Request, response: Response, payload: dict = Body(...)):
        if not _origin_allowed(request.headers.get("origin"), request.headers.get("host")):
            raise HTTPException(403, "Чужой Origin")
        token = payload.get("token")
        if not isinstance(token, str) or len(token) > 512 or not _token_ok(token, settings.auth_token):
            raise HTTPException(401, "Токен не подошёл")
        cookie = secrets.token_urlsafe(32)
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        expires = now_ms() + 8 * 3600_000
        def save():
            with db.batch_writes():
                db.conn.execute("DELETE FROM browser_session WHERE expires_at<=?", (now_ms(),))
                db.conn.execute("INSERT INTO browser_session(token_hash,expires_at,created_at,owner_hash) VALUES(?,?,?,?)", (digest,expires,now_ms(),hashlib.sha256(settings.auth_token.encode()).hexdigest()))
        await asyncio.to_thread(save)
        secure = request.url.scheme == "https" or settings.effective_base_url().startswith("https://")
        response.set_cookie(session_cookie_name, cookie, max_age=8*3600, httponly=True, secure=secure, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True, "expires_at": expires}

    @app.get("/api/auth/session", dependencies=[Depends(require_auth)])
    def session_status():
        return {"authenticated": True}

    @app.delete("/api/auth/session", dependencies=[Depends(require_auth)])
    async def logout(request: Request, response: Response):
        cookie = request.cookies.get(session_cookie_name, "")
        def revoke():
            with db.batch_writes():
                db.conn.execute("DELETE FROM browser_session WHERE token_hash=?", (hashlib.sha256(cookie.encode()).hexdigest(),))
        await asyncio.to_thread(revoke)
        response.delete_cookie(session_cookie_name, path="/")
        return {"authenticated": False}
