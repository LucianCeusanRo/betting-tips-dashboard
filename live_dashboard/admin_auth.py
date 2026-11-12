"""Admin-only token gate (separate from public tip access_gate).

Secret lives in live_dashboard/out/admin_token.txt (or ADMIN_ACCESS_TOKEN env).
Never commit the token file.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TOKEN_FILE = OUT / "admin_token.txt"
OUT.mkdir(parents=True, exist_ok=True)

# Console login comes ONLY from the environment (see .env.example). Empty = login disabled.
_ADMIN_USER = os.environ.get("ADMIN_USER", "")
_ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")


def get_or_create_admin_token() -> str:
    env = (os.environ.get("ADMIN_ACCESS_TOKEN") or "").strip()
    if env:
        if not TOKEN_FILE.exists():
            TOKEN_FILE.write_text(env, encoding="utf-8")
        return env
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def check_admin(token: Optional[str]) -> dict:
    expected = get_or_create_admin_token()
    if not token or token != expected:
        return {
            "ok": False,
            "code": 401,
            "reason": "admin_token_invalid",
            "message_ro": "Consola admin protejata - token invalid sau lipsa.",
        }
    return {"ok": True}


def check_login(username: Optional[str], password: Optional[str]) -> dict:
    """Username/password gate for /admin/login. Returns ok + token on success."""
    u = (username or "").strip()
    p = password or ""
    if not _ADMIN_USER or not _ADMIN_PASS:
        return {"ok": False, "code": 503, "reason": "login_not_configured",
                "message_ro": "Seteaza ADMIN_USER si ADMIN_PASSWORD in mediu."}
    user_ok = hmac.compare_digest(u, _ADMIN_USER)
    pass_ok = hmac.compare_digest(p, _ADMIN_PASS)
    if not (user_ok and pass_ok):
        return {
            "ok": False,
            "code": 401,
            "reason": "bad_credentials",
            "message_ro": "Utilizator sau parola gresita.",
        }
    return {"ok": True, "token": get_or_create_admin_token()}


def check_admin_password(username: Optional[str], password: Optional[str]) -> dict:
    """Name used by admin_routes.py; same env-only check as check_login."""
    return check_login(username, password)
