"""Superbet RO session helper (BOX) — Digi RO egress.

Does NOT place bets or cashout. Credentials from env / .env.local only (never log values).

Env:
  SUPERBET_USER / SUPERBET_EMAIL / SUPERBET_PASSWORD
  SUPERBET_COOKIE_DIR     default state/superbet
  SUPERBET_PROXY / HTTPS_PROXY / HTTP_PROXY / state/egress-proxy.txt
  SUPERBET_HEADLESS       1 (default) | 0
  SUPERBET_OTP_INBOX      optional JSONL for OTP codes
  SUPERBET_OTP_TIMEOUT    seconds to wait for OTP (default 90)

CLI:
  python superbet_session.py check
  python superbet_session.py status
  python superbet_session.py probe
  python superbet_session.py ensure
  python superbet_session.py login
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_COOKIE_DIR = Path(os.environ.get("SUPERBET_COOKIE_DIR", "state/superbet"))
BASE_URL = "https://superbet.ro"
HOME_URL = f"{BASE_URL}/"
ACCOUNT_URL = f"{BASE_URL}/contul-meu"

LOGGED_IN_HINTS = (
    "Contul meu",
    "Deconectare",
    "DECONECTARE",
    "Istoric pariuri",
    "Portofel",
    "Retragere",
)
LOGIN_CTA_HINTS = (
    "Intră în cont",
    "Intra in cont",
    "Autentificare",
    "AUTENTIFICARE",
    "Conectare",
    "Log in",
    "Login",
)


def _now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S EET/EEST")


def _cookie_dir() -> Path:
    d = Path(os.environ.get("SUPERBET_COOKIE_DIR", str(DEFAULT_COOKIE_DIR)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def status_path() -> Path:
    return _cookie_dir() / "status.json"


def storage_state_path() -> Path:
    return _cookie_dir() / "storage_state.json"


def profile_dir() -> Path:
    p = _cookie_dir() / "chrome_profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def need_secret_path() -> Path:
    return _cookie_dir() / "NEED_SECRET"


def otp_inbox_path() -> Path:
    env = os.environ.get("SUPERBET_OTP_INBOX")
    if env:
        return Path(env)
    return _cookie_dir() / "otp_inbox.jsonl"


def _load_env_local() -> None:
    env_local = Path(os.environ.get("SUPERBET_ENV_FILE", str(_cookie_dir() / ".env.local")))
    if not env_local.exists():
        return
    try:
        for raw in env_local.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k.startswith("SUPERBET_") and k not in os.environ and v:
                os.environ[k] = v
    except OSError:
        pass


def get_credentials() -> dict[str, str]:
    _load_env_local()
    user = (
        os.environ.get("SUPERBET_USER")
        or os.environ.get("SUPERBET_EMAIL")
        or os.environ.get("SUPERBET_USERNAME")
        or ""
    ).strip()
    pw = (os.environ.get("SUPERBET_PASSWORD") or "").strip()
    return {"user": user, "password": pw}


def _has_creds() -> bool:
    c = get_credentials()
    return bool(c["user"] and c["password"])


def write_need_secret(reason: str) -> Path:
    p = need_secret_path()
    p.write_text(
        json.dumps(
            {
                "reason": reason,
                "at": _now_label(),
                "hint": "Put SUPERBET_USER + SUPERBET_PASSWORD in .env.local (never chat).",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return p


def clear_need_secret() -> None:
    try:
        need_secret_path().unlink(missing_ok=True)
    except OSError:
        pass


def resolve_proxy() -> Optional[str]:
    proxy_addr = (
        os.environ.get("SUPERBET_PROXY")
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
    return proxy_addr or None


def write_status(data: dict[str, Any]) -> Path:
    row = dict(data)
    row["updated_at"] = time.time()
    row["updated_label"] = _now_label()
    path = status_path()
    path.write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_status() -> Optional[dict[str, Any]]:
    path = status_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def http_check(url: str = HOME_URL, timeout: float = 25.0) -> dict[str, Any]:
    proxy = resolve_proxy()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ro-RO,ro;q=0.9",
        },
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(800)
            code = getattr(resp, "status", None) or resp.getcode()
            final = resp.geturl()
            ok = int(code) in (200, 301, 302) or (body and b"Superbet" in body[:800])
            out: dict[str, Any] = {
                "ok": bool(ok),
                "http_code": int(code),
                "url": final,
                "proxy": proxy,
                "title_hint": "Superbet" if b"Superbet" in body else None,
                "creds_present": _has_creds(),
                "checked_at": _now_label(),
                "mode": "http_check",
            }
    except Exception as exc:
        out = {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc)[:200],
            "proxy": proxy,
            "creds_present": _has_creds(),
            "checked_at": _now_label(),
            "mode": "http_check",
        }
    write_status(out)
    return out


def _headless() -> bool:
    return os.environ.get("SUPERBET_HEADLESS", "1").strip() not in ("0", "false", "False", "no")


def _launch_context(playwright):
    proxy = resolve_proxy()
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir()),
        "headless": _headless(),
        "viewport": {"width": 1400, "height": 900},
        "locale": "ro-RO",
        "timezone_id": "Europe/Bucharest",
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}
        print(f"[superbet_session] using proxy {proxy}", file=sys.stderr)
    chrome = Path("/usr/bin/google-chrome")
    if chrome.exists():
        kwargs["channel"] = "chrome"
    context = playwright.chromium.launch_persistent_context(**kwargs)
    state = storage_state_path()
    if state.exists() and state.stat().st_size > 20:
        try:
            raw = json.loads(state.read_text(encoding="utf-8"))
            cookies = raw.get("cookies") or []
            if cookies:
                context.add_cookies(cookies)
        except Exception as exc:
            print(f"[superbet_session] warn: storage_state load: {exc}", file=sys.stderr)
    return context


def save_storage_state(context) -> Path:
    path = storage_state_path()
    context.storage_state(path=str(path))
    try:
        cookies = context.cookies()
        names = sorted(
            {c.get("name", "") for c in cookies if "superbet" in (c.get("domain") or "")}
        )
        meta = {
            "saved_at": time.time(),
            "saved_label": _now_label(),
            "superbet_cookie_names": names,
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


def _shot(page, name: str) -> Optional[str]:
    path = _cookie_dir() / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


def detect_logged_in(page) -> dict[str, Any]:
    """Strict logged-in detection — FAQ text must not count as balance."""
    url = page.url or ""
    text = _page_text(page)
    text_upper = text.upper()

    has_login_cta = False
    for label in LOGIN_CTA_HINTS:
        try:
            if page.get_by_role("button", name=label).count() > 0:
                has_login_cta = True
                break
            if page.get_by_role("link", name=label).count() > 0:
                has_login_cta = True
                break
        except Exception:
            continue
    if not has_login_cta:
        has_login_cta = any(
            h in text_upper for h in ("INTRĂ ÎN CONT", "INTRA IN CONT", "AUTENTIFICARE")
        )

    has_login_form = False
    try:
        has_login_form = page.locator('input[type="password"]').count() > 0
    except Exception:
        has_login_form = False

    has_account = any(h.upper() in text_upper for h in LOGGED_IN_HINTS)
    has_logout = any(
        x in text_upper
        for x in ("DECONECTARE", "DECONECTEAZĂ", "DECONECTEAZA", "LOG OUT", "LOGOUT")
    )

    balance = None
    for sel in (
        "[data-testid*='balance' i]",
        "[data-qa*='balance' i]",
        "[class*='user-balance' i]",
        "[class*='wallet' i] [class*='amount' i]",
        "header [class*='balance' i]",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=400):
                raw = (loc.inner_text(timeout=400) or "").strip().replace("\n", " ")
                if raw and re.search(r"\d+[.,]\d{2}", raw) and "miza" not in raw.lower():
                    balance = raw[:40]
                    break
        except Exception:
            continue

    cookie_names: set[str] = set()
    try:
        for c in page.context.cookies():
            if "superbet" in (c.get("domain") or ""):
                cookie_names.add(c.get("name") or "")
    except Exception:
        pass
    authish = {
        n.lower()
        for n in cookie_names
        if any(k in n.lower() for k in ("token", "auth", "session", "access", "refresh", "sb-"))
    }

    logged_in = False
    reason = "unknown"
    if has_login_form and not has_account and not balance:
        logged_in, reason = False, "login_form_visible"
    elif has_login_cta and not has_account and not balance and not has_logout:
        logged_in, reason = False, "login_cta_visible"
    elif has_logout or (has_account and not has_login_cta) or (balance and not has_login_cta):
        logged_in = True
        reason = "account_or_balance" if (has_account or balance) else "logout_visible"
    elif authish and not has_login_cta and not has_login_form:
        logged_in, reason = True, "auth_cookie_no_cta"
    elif "contul-meu" in url.lower() or "/account" in url.lower():
        logged_in = not has_login_form and not has_login_cta
        reason = "account_url"
    else:
        logged_in, reason = False, "no_login_markers"

    return {
        "logged_in": logged_in,
        "reason": reason,
        "url": url,
        "has_login_cta": has_login_cta,
        "has_login_form": has_login_form,
        "has_account": has_account,
        "has_logout": has_logout,
        "balance_hint": balance,
        "auth_cookie_count": len(authish),
        "checked_at": _now_label(),
    }


def _dismiss_cookies(page) -> None:
    for pattern in (
        re.compile(r"Accept[aăĂ]", re.I),
        re.compile(r"Accept\s*all", re.I),
        re.compile(r"Accept[aăĂ]\s*tot", re.I),
        re.compile(r"De acord", re.I),
        re.compile(r"Permite tot", re.I),
        re.compile(r"^OK$", re.I),
    ):
        try:
            btn = page.get_by_role("button", name=pattern)
            if btn.count() and btn.first.is_visible(timeout=1000):
                btn.first.click(timeout=3000)
                time.sleep(0.5)
                return
        except Exception:
            continue


def _open_login(page) -> None:
    _dismiss_cookies(page)
    time.sleep(0.5)
    for label in LOGIN_CTA_HINTS:
        for getter in (
            lambda lab=label: page.get_by_role("button", name=lab),
            lambda lab=label: page.get_by_role("link", name=lab),
            lambda lab=label: page.get_by_text(lab, exact=False),
        ):
            try:
                loc = getter()
                if loc and loc.count() and loc.first.is_visible(timeout=1500):
                    loc.first.click(timeout=5000)
                    time.sleep(1.2)
                    return
            except Exception:
                continue
    for url in (f"{BASE_URL}/autentificare", f"{BASE_URL}/login"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(1.0)
            if page.locator('input[type="password"]').count() > 0:
                return
        except Exception:
            continue


def _fill_login(page, user: str, password: str) -> dict[str, Any]:
    """Fill Superbet login modal.

    Fields (confirmed): input[name=usernameOrEmail], input[name=password].
    Labels overlay inputs and intercept clicks — use fill/type with force.
    """
    detail: dict[str, Any] = {"user_filled": False, "pw_filled": False, "submitted": False}
    try:
        if page.locator(".e2e-login-modal, aside.login-modal").count():
            detail["modal"] = True
    except Exception:
        pass

    # Username / email — label intercepts pointer events, so force + fill
    try:
        loc = page.locator('input[name="usernameOrEmail"]').first
        loc.wait_for(state="attached", timeout=10000)
        try:
            loc.click(timeout=2000, force=True)
        except Exception:
            pass
        loc.fill(user, timeout=8000, force=True)
        detail["user_filled"] = True
        detail["user_sel"] = 'input[name="usernameOrEmail"]'
    except Exception as exc:
        detail["user_error"] = type(exc).__name__
        # Fallbacks
        for sel in (
            'input[id^="usernameOrEmail"]',
            'input[autocomplete="username"]',
            'input[type="email"]',
        ):
            try:
                loc = page.locator(sel).first
                loc.fill(user, timeout=5000, force=True)
                detail["user_filled"] = True
                detail["user_sel"] = sel
                break
            except Exception as exc2:
                detail["user_error"] = type(exc2).__name__

    try:
        loc = page.locator('input[name="password"], input[type="password"]').first
        loc.wait_for(state="attached", timeout=10000)
        try:
            loc.click(timeout=2000, force=True)
        except Exception:
            pass
        loc.fill(password, timeout=8000, force=True)
        detail["pw_filled"] = True
        detail["pw_sel"] = "password"
    except Exception as exc:
        detail["pw_error"] = type(exc).__name__

    time.sleep(0.5)
    # Submit
    for name in ("Intră în cont", "Intra in cont", "INTRĂ ÎN CONT", "Autentificare"):
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
            if btn.count():
                try:
                    btn.first.click(timeout=5000)
                except Exception:
                    btn.first.click(timeout=5000, force=True)
                detail["submitted"] = True
                detail["submit_name"] = name
                return detail
        except Exception as exc:
            detail["submit_error"] = type(exc).__name__
    try:
        page.locator('.e2e-login-modal button[type="submit"], button[type="submit"]').first.click(
            timeout=5000, force=True
        )
        detail["submitted"] = True
        detail["submit_name"] = "type=submit"
    except Exception as exc:
        detail["submit_error"] = type(exc).__name__
    return detail



def latest_otp(channel: Optional[str] = None) -> Optional[dict[str, Any]]:
    path = otp_inbox_path()
    if not path.exists():
        return None
    latest = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("used"):
                continue
            if channel and row.get("channel") and row.get("channel") != channel:
                continue
            if row.get("code"):
                latest = row
    except OSError:
        return None
    return latest


def wait_for_otp(
    channel: Optional[str] = None, timeout_sec: int = 90, poll_sec: float = 5.0
) -> Optional[str]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        row = latest_otp(channel=channel)
        if row and row.get("code"):
            return str(row["code"])
        time.sleep(poll_sec)
    return None


def mark_otp_used(code: str) -> None:
    path = otp_inbox_path()
    if not path.exists():
        return
    try:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("code")) == str(code):
                row["used"] = True
                row["used_at"] = _now_label()
            rows.append(row)
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
    except OSError:
        pass


def _otp_challenge_visible(page) -> bool:
    """True only for a real OTP/2FA challenge — not marketing SMS blurbs."""
    try:
        # Dedicated OTP inputs (avoid bare maxlength=6 which matches many fields)
        otp_inputs = page.locator(
            'input[autocomplete="one-time-code"], '
            'input[inputmode="numeric"][maxlength="4"], '
            'input[inputmode="numeric"][maxlength="5"], '
            'input[inputmode="numeric"][maxlength="6"], '
            'input[name*="otp" i], input[name*="smsCode" i], '
            'input[data-testid*="otp" i], input[aria-label*="cod" i]'
        )
        if otp_inputs.count() > 0 and otp_inputs.first.is_visible(timeout=800):
            # Must not be the normal password form alone
            return True
    except Exception:
        pass
    text = _page_text(page).upper()
    strong = (
        "COD DE SECURITATE",
        "CODUL DE SECURITATE",
        "INTRODU CODUL",
        "INTRODUCEȚI CODUL",
        "INTRODUCETI CODUL",
        "ONE-TIME",
        "AUTENTIFICARE ÎN DOI",
        "AUTENTIFICARE IN DOI",
        "VERIFICARE SMS",
        "COD SMS",
        "2FA",
    )
    return any(m in text for m in strong)



def _maybe_handle_otp(page, timeout_sec: Optional[int] = None) -> dict[str, Any]:
    if timeout_sec is None:
        timeout_sec = int(os.environ.get("SUPERBET_OTP_TIMEOUT", "90"))
    if not _otp_challenge_visible(page):
        return {"otp_needed": False}

    shot = _shot(page, "login_otp_challenge")
    print("[superbet_session] OTP challenge — waiting inbox, then stop if missing…", flush=True)
    write_status(
        {
            "state": "waiting_otp",
            "hint": f"Append JSONL code to {otp_inbox_path()}",
            "screenshot": shot,
        }
    )
    code = wait_for_otp(channel=None, timeout_sec=timeout_sec)
    if not code:
        return {
            "otp_needed": True,
            "otp_ok": False,
            "error": "otp_timeout",
            "stopped_at": "otp",
            "screenshot": shot,
            "message": (
                "OTP required for Superbet. Stopped (sleep policy). "
                f"Drop code into {otp_inbox_path()} then re-run login/ensure."
            ),
        }

    try:
        loc = page.locator(
            'input[inputmode="numeric"], input[autocomplete="one-time-code"], '
            'input[name*="otp" i], input[name*="code" i], input[maxlength="6"]'
        ).first
        loc.fill(code, timeout=5000)
        for name in (
            "Confirmă", "Confirma", "Continuă", "Continua",
            "Verifică", "Verifica", "Trimite", "OK", "Autentificare",
        ):
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
    except Exception as exc:
        return {
            "otp_needed": True,
            "otp_ok": False,
            "error": f"otp_fill_failed: {type(exc).__name__}",
            "stopped_at": "otp",
            "screenshot": shot,
        }


def probe_browser(timeout_ms: int = 60000) -> dict[str, Any]:
    proxy = resolve_proxy()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        out = {"ok": False, "error": "playwright_missing", "message": str(exc), "proxy": proxy}
        write_status(out)
        return out

    _load_env_local()
    result: dict[str, Any] = {
        "ok": False,
        "proxy": proxy,
        "creds_present": _has_creds(),
        "mode": "probe_browser",
        "checked_at": _now_label(),
    }
    try:
        with sync_playwright() as p:
            context = _launch_context(p)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                time.sleep(2.0)
                _dismiss_cookies(page)
                time.sleep(0.8)
                title = page.title() or ""
                info = detect_logged_in(page)
                shot = _shot(page, "probe_home")
                result.update(
                    {
                        "ok": "superbet" in title.lower() or "superbet" in (page.url or "").lower(),
                        "title": title[:120],
                        "url": page.url,
                        "screenshot": shot,
                        "logged_in": info.get("logged_in"),
                        "reason": info.get("reason"),
                        "has_login_cta": info.get("has_login_cta"),
                        "balance_hint": info.get("balance_hint"),
                    }
                )
                save_storage_state(context)
            finally:
                context.close()
    except Exception as exc:
        result["error"] = type(exc).__name__
        result["message"] = str(exc)[:240]
    write_status(result)
    return result


def check_session(timeout_ms: int = 90000) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    proxy = resolve_proxy()
    with sync_playwright() as p:
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            time.sleep(2.0)
            _dismiss_cookies(page)
            time.sleep(0.8)
            info = detect_logged_in(page)
            shot = _shot(page, "check_home")
            save_storage_state(context)
            out = {
                "ok": bool(info.get("logged_in")),
                "state": "logged_in" if info.get("logged_in") else "logged_out",
                "proxy": proxy,
                "creds_present": _has_creds(),
                "mode": "check_session",
                "screenshot": shot,
                **info,
            }
            write_status(out)
            return out
        finally:
            context.close()


def login(timeout_ms: int = 90000) -> dict[str, Any]:
    creds = get_credentials()
    if not creds["password"] or not creds["user"]:
        p = write_need_secret(
            "SUPERBET_PASSWORD missing" if not creds["password"] else "SUPERBET_USER/EMAIL missing"
        )
        return {
            "ok": False,
            "error": "NEED_SECRET",
            "need_secret": str(p),
            "message": "SUPERBET_USER + SUPERBET_PASSWORD required in .env.local",
        }

    from playwright.sync_api import sync_playwright

    proxy = resolve_proxy()
    with sync_playwright() as p:
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            time.sleep(2.0)
            _dismiss_cookies(page)
            time.sleep(0.8)
            pre = detect_logged_in(page)
            _shot(page, "login_pre")
            if pre.get("logged_in"):
                save_storage_state(context)
                clear_need_secret()
                out = {"ok": True, "already_logged_in": True, "proxy": proxy, "via": "already", **pre}
                write_status({"state": "logged_in", **out})
                return out

            _open_login(page)
            time.sleep(1.2)
            _shot(page, "login_form")
            fill = _fill_login(page, creds["user"], creds["password"])
            time.sleep(3.0)
            _shot(page, "login_after_submit")

            otp = _maybe_handle_otp(page)
            if otp.get("otp_needed") and not otp.get("otp_ok"):
                save_storage_state(context)
                out = {"ok": False, "proxy": proxy, "fill": fill, "stopped_at": "otp", **otp}
                write_status({"state": "otp_blocked", **out})
                return out

            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            time.sleep(2.0)
            _dismiss_cookies(page)
            info = detect_logged_in(page)
            shot = _shot(page, "login_post")
            save_storage_state(context)
            if info.get("logged_in"):
                clear_need_secret()
                out = {
                    "ok": True,
                    "proxy": proxy,
                    "via": "login",
                    "fill": fill,
                    "otp": otp,
                    **info,
                }
                write_status({"state": "logged_in", **out})
                return out

            if _otp_challenge_visible(page) or info.get("has_login_form"):
                out = {
                    "ok": False,
                    "error": "otp_or_login_pending",
                    "stopped_at": "otp" if _otp_challenge_visible(page) else "login_form",
                    "proxy": proxy,
                    "fill": fill,
                    "otp": otp,
                    "screenshot": shot,
                    **info,
                }
                write_status(
                    {
                        "state": "otp_blocked" if out["stopped_at"] == "otp" else "login_failed",
                        **out,
                    }
                )
                return out

            out = {
                "ok": False,
                "error": "login_failed",
                "proxy": proxy,
                "fill": fill,
                "otp": otp,
                "screenshot": shot,
                **info,
            }
            write_status({"state": "login_failed", **out})
            return out
        finally:
            context.close()


def ensure_session() -> dict[str, Any]:
    reach = http_check()
    if not reach.get("ok"):
        return {
            "ok": False,
            "action": "reachability",
            "error": "superbet_unreachable",
            "reachability": reach,
        }
    info = check_session()
    if info.get("logged_in"):
        return {"ok": True, "action": "none", **info}
    print("[superbet_session] logged out — attempting login…", flush=True)
    result = login()
    result["action"] = "login"
    result["reachability"] = {
        "ok": reach.get("ok"),
        "http_code": reach.get("http_code"),
        "proxy": reach.get("proxy"),
    }
    return result


def cmd_status() -> dict[str, Any]:
    existing = read_status()
    if existing:
        return existing
    return http_check()


def _safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _safe(v)
            for k, v in obj.items()
            if "pass" not in k.lower() and "secret" not in k.lower() and k != "password"
        }
    if isinstance(obj, list):
        return [_safe(x) for x in obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description="Superbet RO session (Digi egress)")
    ap.add_argument(
        "cmd",
        nargs="?",
        default="status",
        choices=["check", "status", "probe", "ensure", "login"],
    )
    args = ap.parse_args()

    if args.cmd == "check":
        r = http_check()
    elif args.cmd == "probe":
        r = probe_browser()
    elif args.cmd == "login":
        r = login()
    elif args.cmd == "ensure":
        r = ensure_session()
    else:
        r = cmd_status()

    safe = _safe(r)
    print(json.dumps(safe, indent=2, ensure_ascii=False))
    if args.cmd in ("ensure", "login"):
        return 0 if safe.get("ok") or safe.get("logged_in") else 1
    if args.cmd == "check":
        return 0 if safe.get("ok") else 1
    return 0 if safe.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
