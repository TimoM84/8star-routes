"""Authentication.

- Administrator: password login (APP_PASSWORD) -> signed, HttpOnly session cookie.
- Read-only viewers: `Authorization: Bearer <API_READ_TOKEN>` for the API, or
  an embed token (EMBED_TOKEN) for the Home Assistant dashboard, sent by the
  page as the `X-Embed-Token` header (no third-party cookie needed).
- AUTH_MODE=none disables login entirely; only use it behind an
  authenticating reverse proxy.

State-changing requests must carry `X-Requested-With: 8star-routes`, which a
cross-site form cannot send without a CORS preflight (CSRF protection).
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import Config

COOKIE = "routes_session"
SESSION_MAX_AGE = 30 * 86400
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "8star-routes"


class Auth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        secret = cfg.session_secret or secrets.token_hex(32)
        self.ephemeral_secret = not cfg.session_secret
        self.signer = TimestampSigner(secret, salt="routes-session")
        self._attempts: dict[str, deque] = defaultdict(deque)

    # --- login ---------------------------------------------------------------------
    def rate_limited(self, ip: str) -> bool:
        q = self._attempts[ip]
        now = time.monotonic()
        while q and now - q[0] > 300:
            q.popleft()
        return len(q) >= 10

    def check_password(self, ip: str, password: str) -> bool:
        self._attempts[ip].append(time.monotonic())
        return bool(self.cfg.app_password) and hmac.compare_digest(password.encode(), self.cfg.app_password.encode())

    def issue(self) -> str:
        return self.signer.sign(b"admin").decode()

    def cookie_secure(self, request: Request) -> bool:
        if self.cfg.cookie_secure == "auto":
            return request.url.scheme == "https"
        return self.cfg.cookie_secure == "true"

    # --- roles ---------------------------------------------------------------------
    def role(self, request: Request) -> str | None:
        if self.cfg.auth_mode == "none":
            return "admin"
        tok = request.cookies.get(COOKIE)
        if tok:
            try:
                if self.signer.unsign(tok, max_age=SESSION_MAX_AGE) == b"admin":
                    return "admin"
            except (BadSignature, SignatureExpired):
                pass
        authz = request.headers.get("authorization", "")
        if self.cfg.api_read_token and authz.lower().startswith("bearer ") and \
                hmac.compare_digest(authz[7:].strip().encode(), self.cfg.api_read_token.encode()):
            return "viewer"
        emb = request.headers.get("x-embed-token") or ""
        if self.cfg.embed_token and emb and hmac.compare_digest(emb.encode(), self.cfg.embed_token.encode()):
            return "viewer"
        return None

    def require_viewer(self, request: Request) -> str:
        r = self.role(request)
        if r is None:
            raise HTTPException(401, "Login required")
        return r

    def require_admin(self, request: Request) -> str:
        r = self.role(request)
        if r is None:
            raise HTTPException(401, "Login required")
        if r != "admin":
            raise HTTPException(403, "Administrator login required")
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "Missing X-Requested-With header")
        return r
