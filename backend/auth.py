"""单进程本地主机认证；启动 secret 只允许兑换一次。"""

import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response


COOKIE_NAME = "reactron_host_session"
_bootstrap_secret: str | None = None
_session_token: str | None = None
_session_origin: str | None = None


@dataclass(frozen=True)
class Principal:
    actor_id: str = "local-host"


def issue_bootstrap_secret() -> str:
    """启动时生成一次性 secret；调用者仅在本地终端展示。"""
    global _bootstrap_secret, _session_token, _session_origin
    _bootstrap_secret = secrets.token_urlsafe(32)
    _session_token = None
    _session_origin = None
    return _bootstrap_secret


def _request_origin(request: Request, *, require_origin: bool) -> str:
    host = request.headers.get("host", "")
    if not host or any(char in host for char in "/\\@?# \t\r\n"):
        raise HTTPException(403, "forbidden")
    try:
        parsed = urlsplit(f"{request.url.scheme}://{host}")
        address = parsed.hostname or ""
        local = address == "localhost" or ipaddress.ip_address(address).is_loopback
        if not local or parsed.port is None:
            raise ValueError("non-local host or missing port")
    except ValueError as exc:
        raise HTTPException(403, "forbidden") from exc
    origin = f"{request.url.scheme}://{host}"
    if require_origin and request.headers.get("origin") != origin:
        raise HTTPException(403, "forbidden")
    return origin


def bootstrap(request: Request, response: Response, secret: str) -> None:
    """一次性兑换本地主机 cookie；无有效 secret 时不触及 session。"""
    global _bootstrap_secret, _session_token, _session_origin
    origin = _request_origin(request, require_origin=True)
    if not _bootstrap_secret or not hmac.compare_digest(secret, _bootstrap_secret):
        raise HTTPException(401, "unauthenticated")
    _bootstrap_secret = None
    _session_token = secrets.token_urlsafe(32)
    _session_origin = origin
    response.set_cookie(
        COOKIE_NAME,
        _session_token,
        path="/",
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )


def require_host_session(request: Request) -> Principal:
    """保护 API；所有变更请求额外检查精确 Origin。"""
    origin = _request_origin(
        request,
        require_origin=request.method.upper() not in {"GET", "HEAD", "OPTIONS"},
    )
    token = request.cookies.get(COOKIE_NAME, "")
    if not _session_token or not token or not hmac.compare_digest(token, _session_token):
        raise HTTPException(401, "unauthenticated")
    if origin != _session_origin:
        raise HTTPException(403, "forbidden")
    return Principal()
