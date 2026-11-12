"""Betano RO session keep-alive + auto-relogin (BOX only).

Does NOT place bets. Reads credentials from env only (never chat).

Env:
  BETANO_PASSWORD          required for auto-login
  BETANO_USER / BETANO_EMAIL / BETANO_USERNAME   login id (email or username)
  BETANO_COOKIE_DIR        default state/betano
  BETANO_OTP_INBOX         JSONL path for SMS/email OTP codes
  BETANO_HEADLESS          1 (default) | 0
  BETANO_KEEPALIVE_SEC     default 3000 (~50 min)

CLI:
  python betano_session.py check
  python betano_session.py ensure
  python betano_session.py login
  python betano_session.py keepalive [--once]
  python betano_session.py status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths (BOX)
# ---------------------------------------------------------------------------
DEFAULT_COOKIE_DIR = Path(os.environ.get("BETANO_COOKIE_DIR", "state/betano"))
BASE_URL = "https://ro.betano.com"
HOME_URL = f"{BASE_URL}/"
OPEN_BETS_URL = f"{BASE_URL}/myaccount/bethistory/open/"
LOGIN_HINTS = ("CONECTARE", "Autentificare", "AUTENTIFICARE", "ID de autentificare")
LOGGED_IN_HINTS = ("Contul meu", "Sold", "Istoric pariuri", "Deconectare", "DECONECTARE")

# Europe/Bucharest for human logs (box clock is already that zone)
def _now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S EET/EEST")


def _cookie_dir() -> Path:
    d = Path(os.environ.get("BETANO_COOKIE_DIR", str(DEFAULT_COOKIE_DIR)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def storage_state_path() -> Path:
    return _cookie_dir() / "storage_state.json"


def profile_dir() -> Path:
    p = _cookie_dir() / "chrome_profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def status_path() -> Path:
    return _cookie_dir() / "status.json"


def need_secret_path() -> Path:
    return _cookie_dir() / "NEED_SECRET"


def otp_inbox_path() -> Path:
    env = os.environ.get("BETANO_OTP_INBOX")
    if env:
        return Path(env)
    # Prefer box cookie dir; also accept live_dashboard/out when co-located
    candidates = [
        _cookie_dir() / "otp_inbox.jsonl",
        Path(__file__).resolve().parent / "out" / "betano_otp_inbox.jsonl",
        Path("state/betano/otp_inbox.jsonl"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


# ---------------------------------------------------------------------------
# Credentials / secret gate
# ---------------------------------------------------------------------------
def _load_vault_secrets() -> None:
    """Fill missing BETANO_* from box card vault / .env.local. Never logs values."""
    env_local = Path(os.environ.get("BETANO_ENV_FILE", "state/betano/.env.local"))
    if env_local.exists():
        try:
            for raw in env_local.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'\"", '"'):
                    v = v[1:-1]
                if k.startswith("BETANO_") and k not in os.environ and v:
                    os.environ[k] = v
        except OSError:
            pass
    vault = Path(os.environ.get("BETANO_SECRETS_JSON", "state/secrets.json"))
    if vault.exists():
        try:
            card = (json.loads(vault.read_text(encoding="utf-8")).get("card") or {})
            for k, v in card.items():
                if k.startswith("BETANO_") and isinstance(v, str) and v:
                    os.environ[k] = v  # vault wins
        except (OSError, json.JSONDecodeError, TypeError):
            pass



def _is_access_restricted(page) -> bool:
    try:
        title = (page.title() or "").lower()
        body = ""
        try:
            body = (page.inner_text("body") or "")[:2000].lower()
        except Exception:
            body = ""
        blob = title + " " + body + " " + (page.url or "").lower()
        return (
            "access to this page is restricted" in blob
            or "betano splash screen" in title
            or "security and compliance" in blob
        )
    except Exception:
        return False


def get_credentials() -> dict[str, Optional[str]]:
    _load_vault_secrets()
    password = os.environ.get("BETANO_PASSWORD") or ""
    user = (
        os.environ.get("BETANO_USER")
        or os.environ.get("BETANO_EMAIL")
        or os.environ.get("BETANO_USERNAME")
        or ""
    ).strip()
    return {"user": user or None, "password": password or None}


def write_need_secret(reason: str = "BETANO_PASSWORD missing from os.environ") -> Path:
    p = need_secret_path()
    payload = {
        "ts": time.time(),
        "ts_label": _now_label(),
        "reason": reason,
        "needed": ["BETANO_PASSWORD", "BETANO_USER or BETANO_EMAIL"],
        "how": "secret-request / parent inject into box env — never paste password in chat",
        "message_parent": (
            "NEED_SECRET: Betano keep-alive scaffolded but cannot auto-login. "
            "Please inject BETANO_PASSWORD (and BETANO_USER/BETANO_EMAIL) into the box env."
        ),
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Mirror next to this module for easy discovery
    mirror = Path(__file__).resolve().parent / "NEED_SECRET"
    try:
        mirror.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    return p


def clear_need_secret() -> None:
    for p in (need_secret_path(), Path(__file__).resolve().parent / "NEED_SECRET"):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
def write_status(data: dict[str, Any]) -> None:
    data = dict(data)
    data["updated_at"] = time.time()
    data["updated_label"] = _now_label()
    status_path().write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_status() -> dict[str, Any]:
    p = status_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OTP inbox (SMS via /admin/otp; email via parent Gmail MCP hook)
# ---------------------------------------------------------------------------
def latest_otp(channel: Optional[str] = None, max_age_sec: int = 600) -> Optional[dict]:
    path = otp_inbox_path()
    if not path.exists():
        return None
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    now = time.time()
    for row in reversed(rows):
        if row.get("used"):
            continue
        if channel and row.get("channel") != channel:
            continue
        if now - float(row.get("ts") or 0) > max_age_sec:
            continue
        return row
    return None


def mark_otp_used(code: str) -> bool:
    path = otp_inbox_path()
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    changed = False
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            out.append(line)
            continue
        if not changed and not row.get("used") and str(row.get("code")) == str(code):
            row["used"] = True
            row["used_at"] = time.time()
            changed = True
        out.append(json.dumps(row, ensure_ascii=False))
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def append_otp_local(code: str, channel: str = "email", note: str = "") -> dict:
    """Parent/Gmail hook can call this (or drop JSONL) so box login can consume OTP."""
    code = "".join(c for c in str(code or "") if c.isalnum())
    if len(code) < 4:
        return {"ok": False, "error": "code_too_short"}
    path = otp_inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "channel": channel, "code": code, "used": False, "note": note[:200]}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(path), "channel": channel}


def wait_for_otp(
    channel: Optional[str] = None,
    timeout_sec: int = 180,
    poll_sec: float = 5.0,
) -> Optional[str]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        row = latest_otp(channel=channel)
        if row and row.get("code"):
            return str(row["code"])
        time.sleep(poll_sec)
    return None


# ---------------------------------------------------------------------------
# Playwright browser helpers
# ---------------------------------------------------------------------------
def _headless() -> bool:
    return os.environ.get("BETANO_HEADLESS", "1").strip() not in ("0", "false", "False", "no")


def _launch_context(playwright):
    """Persistent Chrome profile + storage_state merge for durable cookies."""
    from playwright.sync_api import ViewportSize  # noqa: F401 — type hint only if needed

    user_data = str(profile_dir())
    state = storage_state_path()
    # Prefer a local egress proxy (Romanian IP) when available — Betano geo-blocks US box egress.
    proxy_addr = (
        os.environ.get("BETANO_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    if not proxy_addr:
        eg = Path("state/egress-proxy.txt")
        if eg.exists():
            proxy_addr = eg.read_text().strip().splitlines()[0].strip()
        elif os.environ.get("EGRESS_PROXY_ADDR"):
            proxy_addr = os.environ["EGRESS_PROXY_ADDR"].strip()
    if proxy_addr and not proxy_addr.startswith("http"):
        proxy_addr = f"http://{proxy_addr}"

    kwargs: dict[str, Any] = {
        "user_data_dir": user_data,
        "headless": _headless(),
        "viewport": {"width": 1400, "height": 900},
        "locale": "ro-RO",
        "timezone_id": "Europe/Bucharest",
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy_addr:
        kwargs["proxy"] = {"server": proxy_addr}
        print(f"[betano_session] using proxy {proxy_addr}", file=sys.stderr)
    # Chromium channel if system Chrome present
    chrome = Path("/usr/bin/google-chrome")
    if chrome.exists():
        kwargs["channel"] = "chrome"
    context = playwright.chromium.launch_persistent_context(**kwargs)
    if state.exists() and state.stat().st_size > 20:
        try:
            raw = json.loads(state.read_text(encoding="utf-8"))
            cookies = raw.get("cookies") or []
            if cookies:
                context.add_cookies(cookies)
        except Exception as e:
            print(f"[betano_session] warn: could not load storage_state: {e}", file=sys.stderr)
    return context


def save_storage_state(context) -> Path:
    path = storage_state_path()
    context.storage_state(path=str(path))
    # Also dump a small cookie summary (names only — no values in logs)
    try:
        cookies = context.cookies()
        names = sorted({c.get("name", "") for c in cookies if "betano" in (c.get("domain") or "")})
        meta = {
            "saved_at": time.time(),
            "saved_label": _now_label(),
            "betano_cookie_names": names,
            "cookie_count": len(cookies),
            "path": str(path),
        }
        (_cookie_dir() / "cookies_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        pass
    return path


def _page_text(page, limit: int = 8000) -> str:
    try:
        return (page.inner_text("body") or "")[:limit]
    except Exception:
        try:
            return (page.content() or "")[:limit]
        except Exception:
            return ""


def detect_logged_in(page) -> dict[str, Any]:
    """Heuristic: CONECTARE absent + logged-in markers or account URL works."""
    url = page.url or ""
    text = _page_text(page)
    text_upper = text.upper()

    has_conectare = "CONECTARE" in text_upper
    # Header login CTA often a button/link
    try:
        conectare_vis = page.get_by_role("link", name="CONECTARE").count() > 0
        conectare_vis = conectare_vis or page.get_by_role("button", name="CONECTARE").count() > 0
    except Exception:
        conectare_vis = False
    has_conectare = has_conectare or conectare_vis

    has_login_form = ("ID DE AUTENTIFICARE" in text_upper) or ("AUTENTIFICARE" in text_upper and "PAROLA" in text_upper)
    has_account = any(h.upper() in text_upper for h in LOGGED_IN_HINTS)

    balance = None
    for sel in (
        "[class*='balance']",
        "[data-qa*='balance']",
        "text=/\\d+[.,]\\d{2}\\s*RON/i",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=500):
                balance = (loc.inner_text(timeout=500) or "").strip()[:40]
                if balance:
                    break
        except Exception:
            continue

    # Cookie signal
    cookie_names = set()
    try:
        for c in page.context.cookies():
            if "betano" in (c.get("domain") or ""):
                cookie_names.add(c.get("name") or "")
    except Exception:
        pass
    has_gauth = "GAUTH" in cookie_names or "gauth" in {n.lower() for n in cookie_names}

    logged_in = False
    reason = "unknown"
    if has_login_form and not has_account:
        logged_in = False
        reason = "login_form_visible"
    elif has_conectare and not has_account and not balance:
        logged_in = False
        reason = "conectare_visible"
    elif has_account or balance or (has_gauth and not has_conectare):
        logged_in = True
        reason = "account_or_balance" if (has_account or balance) else "gauth_cookie"
    elif not has_conectare and "betano" in url and not has_login_form:
        # Soft positive: home without CONECTARE often means session alive
        logged_in = True
        reason = "no_conectare_on_home"
    else:
        logged_in = False
        reason = "no_login_markers"

    return {
        "logged_in": logged_in,
        "reason": reason,
        "url": url,
        "has_conectare": has_conectare,
        "has_login_form": has_login_form,
        "has_account": has_account,
        "balance_hint": balance,
        "has_gauth": has_gauth,
        "checked_at": _now_label(),
    }


def _dismiss_cookies(page) -> None:
    for label in ("Accept", "Acceptă", "ACCEPTĂ", "Accept all", "De acord", "OK"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible(timeout=800):
                btn.first.click(timeout=2000)
                time.sleep(0.4)
                return
        except Exception:
            continue


def _open_login(page) -> None:
    _dismiss_cookies(page)
    # Prefer header CONECTARE
    for getter in (
        lambda: page.get_by_role("link", name="CONECTARE"),
        lambda: page.get_by_role("button", name="CONECTARE"),
        lambda: page.locator("text=CONECTARE").first,
        lambda: page.locator("a[href*='login'], button:has-text('CONECTARE')").first,
    ):
        try:
            loc = getter()
            if loc and loc.count() and loc.first.is_visible(timeout=1500):
                loc.first.click(timeout=5000)
                time.sleep(1.0)
                return
        except Exception:
            continue
    # Direct login URL fallback
    page.goto(f"{BASE_URL}/login/", wait_until="domcontentloaded", timeout=60000)


def _fill_login(page, user: str, password: str) -> None:
    # Prefer email tab when user looks like email
    if "@" in user:
        for name in ("E-mail", "Email", "e-mail"):
            try:
                tab = page.get_by_role("button", name=name)
                if tab.count():
                    tab.first.click(timeout=3000)
                    time.sleep(0.3)
                    break
                tab = page.get_by_text(name, exact=True)
                if tab.count():
                    tab.first.click(timeout=3000)
                    time.sleep(0.3)
                    break
            except Exception:
                continue
    else:
        for name in ("Nume de utilizator", "Username"):
            try:
                tab = page.get_by_role("button", name=name)
                if tab.count():
                    tab.first.click(timeout=3000)
                    time.sleep(0.3)
                    break
            except Exception:
                continue

    # Username / email field
    filled_user = False
    for sel in (
        'input[type="email"]',
        'input[name*="user" i]',
        'input[name*="email" i]',
        'input[placeholder*="utilizator"]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="ex." i]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1000):
                loc.fill(user, timeout=5000)
                filled_user = True
                break
        except Exception:
            continue
    if not filled_user:
        # First visible text input in form
        page.locator('input[type="text"], input:not([type])').first.fill(user, timeout=5000)

    # Password
    pw = page.locator('input[type="password"]').first
    pw.fill(password, timeout=5000)

    # Submit
    for name in ("AUTENTIFICARE", "Autentificare", "Conectare", "CONECTARE"):
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count():
                btn.first.click(timeout=5000)
                return
        except Exception:
            continue
    page.locator('button[type="submit"]').first.click(timeout=5000)


def _maybe_handle_otp(page, timeout_sec: int = 180) -> dict[str, Any]:
    """If OTP challenge appears, wait for code in otp inbox (SMS or email)."""
    text = _page_text(page).upper()
    otp_markers = ("COD", "OTP", "VERIFIC", "SMS", "ONE-TIME", "AUTENTIFICARE ÎN DOI", "2FA")
    if not any(m in text for m in otp_markers):
        # Also check input patterns
        try:
            if page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]').count() == 0:
                return {"otp_needed": False}
        except Exception:
            return {"otp_needed": False}

    print("[betano_session] OTP challenge detected — waiting for inbox code…", flush=True)
    write_status({"state": "waiting_otp", "hint": "SMS via /admin/otp or email via Gmail MCP → otp_inbox"})
    code = wait_for_otp(channel=None, timeout_sec=timeout_sec)
    if not code:
        return {
            "otp_needed": True,
            "otp_ok": False,
            "error": "otp_timeout",
            "message": (
                "OTP required. SMS: submit at /admin/otp then sync to "
                f"{otp_inbox_path()}. Email: parent Gmail MCP → append_otp_local."
            ),
        }

    # Fill OTP
    try:
        loc = page.locator(
            'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="code" i]'
        ).first
        loc.fill(code, timeout=5000)
        for name in ("Confirmă", "Confirma", "Continuă", "Continua", "Verifică", "Verifica", "Trimite", "OK", "AUTENTIFICARE"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.count():
                    btn.first.click(timeout=5000)
                    break
            except Exception:
                continue
        mark_otp_used(code)
        time.sleep(2.0)
        return {"otp_needed": True, "otp_ok": True, "channel_used": "inbox"}
    except Exception as e:
        return {"otp_needed": True, "otp_ok": False, "error": f"otp_fill_failed: {e}"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def check_session() -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(1.5)
            _dismiss_cookies(page)
            info = detect_logged_in(page)
            # Soft confirm via open bets page (no bet placement)
            if info.get("logged_in"):
                try:
                    page.goto(OPEN_BETS_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(1.0)
                    t = _page_text(page).upper()
                    if "CONECTARE" in t or "ID DE AUTENTIFICARE" in t:
                        info["logged_in"] = False
                        info["reason"] = "open_bets_redirected_to_login"
                    else:
                        info["open_bets_ok"] = True
                except Exception as e:
                    info["open_bets_error"] = str(e)[:200]
            save_storage_state(context)
            write_status({"state": "logged_in" if info["logged_in"] else "logged_out", **info})
            return info
        finally:
            context.close()


def login() -> dict[str, Any]:
    creds = get_credentials()
    if not creds["password"]:
        p = write_need_secret("BETANO_PASSWORD missing")
        return {
            "ok": False,
            "error": "NEED_SECRET",
            "need_secret": str(p),
            "message": "BETANO_PASSWORD not in env — scaffolding only; parent must inject secret.",
        }
    if not creds["user"]:
        p = write_need_secret("BETANO_USER/BETANO_EMAIL missing")
        return {
            "ok": False,
            "error": "NEED_SECRET",
            "need_secret": str(p),
            "message": "BETANO_USER or BETANO_EMAIL required with password.",
        }

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(1.2)
            _dismiss_cookies(page)
            pre = detect_logged_in(page)
            if pre.get("logged_in"):
                save_storage_state(context)
                clear_need_secret()
                write_status({"state": "logged_in", "via": "already", **pre})
                return {"ok": True, "already_logged_in": True, **pre}

            _open_login(page)
            time.sleep(1.0)
            _fill_login(page, creds["user"], creds["password"])
            time.sleep(2.5)
            otp = _maybe_handle_otp(page)
            if otp.get("otp_needed") and not otp.get("otp_ok"):
                write_status({"state": "otp_blocked", **otp})
                return {"ok": False, **otp}

            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(1.5)
            info = detect_logged_in(page)
            save_storage_state(context)
            if info.get("logged_in"):
                clear_need_secret()
                write_status({"state": "logged_in", "via": "login", **info})
                return {"ok": True, **info, "otp": otp}
            write_status({"state": "login_failed", **info, "otp": otp})
            # Screenshot for parent debug (no secrets)
            shot = _cookie_dir() / "last_login_fail.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
            except Exception:
                shot = None
            return {"ok": False, "error": "login_failed", **info, "screenshot": str(shot) if shot else None}
        finally:
            context.close()


def ensure_session() -> dict[str, Any]:
    """Check; if logged out, login with env creds."""
    info = check_session()
    if info.get("logged_in"):
        return {"ok": True, "action": "none", **info}
    print("[betano_session] logged out — attempting login…", flush=True)
    result = login()
    result["action"] = "login"
    return result


def keepalive_ping() -> dict[str, Any]:
    """Lightweight GET while cookies valid; re-login if session dead."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(0.8)
            info = detect_logged_in(page)
            save_storage_state(context)
            if info.get("logged_in"):
                write_status({"state": "keepalive_ok", **info})
                return {"ok": True, "action": "ping", **info}
        finally:
            context.close()

    # Session dead → ensure
    print("[betano_session] keepalive detected logout — ensure_session()", flush=True)
    return ensure_session()


def keepalive_loop(interval_sec: Optional[int] = None) -> None:
    interval = int(interval_sec or os.environ.get("BETANO_KEEPALIVE_SEC", "3000"))
    print(f"[betano_session] keepalive loop every {interval}s (~{interval/60:.0f} min)", flush=True)
    while True:
        try:
            r = keepalive_ping()
            print(json.dumps({"ts": _now_label(), **{k: r.get(k) for k in ('ok', 'logged_in', 'action', 'reason', 'error')}}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(f"[betano_session] keepalive error: {e}", file=sys.stderr, flush=True)
            write_status({"state": "keepalive_error", "error": str(e)[:300]})
        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Betano session keep-alive (no betting)")
    parser.add_argument(
        "command",
        choices=["check", "ensure", "login", "keepalive", "status", "append-otp"],
        help="Action",
    )
    parser.add_argument("--once", action="store_true", help="keepalive: single ping")
    parser.add_argument("--interval", type=int, default=0, help="keepalive loop seconds")
    parser.add_argument("--code", default="", help="append-otp: OTP code")
    parser.add_argument("--channel", default="email", help="append-otp channel")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(read_status() or {"state": "no_status_yet"}, indent=2, ensure_ascii=False))
        ns = need_secret_path()
        if ns.exists():
            print("--- NEED_SECRET ---")
            print(ns.read_text(encoding="utf-8"))
        return 0

    if args.command == "append-otp":
        r = append_otp_local(args.code, channel=args.channel)
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 2

    # Gate: for login/ensure, require secret or write NEED_SECRET and stop
    if args.command in ("login", "ensure"):
        creds = get_credentials()
        if not creds["password"] or not creds["user"]:
            p = write_need_secret(
                "BETANO_PASSWORD missing" if not creds["password"] else "BETANO_USER/EMAIL missing"
            )
            print(json.dumps({
                "ok": False,
                "error": "NEED_SECRET",
                "need_secret": str(p),
                "message_parent": (
                    "NEED_SECRET: Betano keep-alive scaffold ready on box. "
                    "Inject BETANO_PASSWORD + BETANO_USER/BETANO_EMAIL into box env via secret-request. "
                    "Do not paste password in chat."
                ),
            }, indent=2, ensure_ascii=False))
            return 3

    if args.command == "check":
        r = check_session()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r.get("logged_in") else 1

    if args.command == "login":
        r = login()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if args.command == "ensure":
        r = ensure_session()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if args.command == "keepalive":
        if args.once:
            r = keepalive_ping()
            print(json.dumps(r, indent=2, ensure_ascii=False))
            return 0 if r.get("ok") else 1
        # Before looping: if no secret and no cookies, write NEED_SECRET and exit
        creds = get_credentials()
        if not storage_state_path().exists() and (not creds["password"] or not creds["user"]):
            p = write_need_secret("No cookies yet and BETANO_PASSWORD missing — cannot keepalive")
            print(json.dumps({"ok": False, "error": "NEED_SECRET", "need_secret": str(p)}, indent=2))
            return 3
        keepalive_loop(args.interval or None)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
