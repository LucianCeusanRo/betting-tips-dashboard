"""Betano RO web cashout via Playwright (box worker).

Selectors + timing are documented inline below.

Flow:
  1. Open home / open-bets UI → PARIURILE MELE → Deschise (optional Cash Out filter).
  2. Match ticket by stake_marker (2,59 / 2.59) and optional team substring.
  3. Click dark button.GTM-cashoutButton (data-qa=cashout-button).
  4. Wait until button.GTM-cashoutConfirm exists.
  5. Wait ~350–500ms (pointer-events guard).
  6. Re-query .GTM-cashoutConfirm and Playwright-click GREEN confirm —
     NEVER .GTM-cashoutCancel. Do not freeze setTimeout; do not use console .click().
  7. Success: "Cash Out reușit" / ticket leaves Deschise.

Does not place bets. Safe to import without launching a browser.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

# --- Skill selectors ---------------------------------------------------------
CASHOUT_BUTTON_SEL = 'button.GTM-cashoutButton, button[data-qa="cashout-button"]'
CASHOUT_CONFIRM_SEL = "button.GTM-cashoutConfirm"
CASHOUT_CANCEL_SEL = "button.GTM-cashoutCancel"  # never click
CONFIRM_GUARD_MS_DEFAULT = 400  # skill: ~350–500ms after confirm appears
CONFIRM_WAIT_MS = 2500  # stay under ~2.5–3s offer window
SUCCESS_HINTS = (
    "Cash Out reușit",
    "Cash Out reusit",
    "Cash out reușit",
    "Cash out reusit",
)
ODDS_CHANGED_HINTS = (
    "Cotele s-au schimbat",
    "cotele s-au schimbat",
    "Odds have changed",
)

# Amounts like 2.59 / 2,59 / 12.5 (not bare ticket ids)
_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,4}[.,]\d{1,2})(?!\d)")


def stake_text_variants(marker: float) -> list[str]:
    """Display forms for stake matching (dot and comma decimals)."""
    try:
        m = float(marker)
    except (TypeError, ValueError):
        return []
    # Normalize to 2 dp when close to cents; also 1 dp / int-ish
    forms: list[str] = []
    for fmt in ("{:.2f}", "{:.1f}", "{:.0f}"):
        dot = fmt.format(m)
        # drop trailing .0 for int form already handled by :.0f
        if fmt != "{:.0f}" and dot.endswith("0") and m == int(m):
            continue
        forms.append(dot)
        forms.append(dot.replace(".", ","))
    # Always include exact 2dp both separators (primary marker style)
    two = f"{m:.2f}"
    forms.extend([two, two.replace(".", ",")])
    # unique preserve order
    out: list[str] = []
    seen: set[str] = set()
    for f in forms:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def parse_amounts(text: str) -> list[float]:
    """Extract decimal amounts from UI text (comma or dot)."""
    found: list[float] = []
    for raw in _AMOUNT_RE.findall(text or ""):
        try:
            found.append(float(raw.replace(",", ".")))
        except ValueError:
            continue
    return found


def text_has_stake(text: str, marker: float, *, tol: float = 0.009) -> bool:
    """True if text contains an amount matching stake_marker (2.59 ≈ 2,59)."""
    try:
        target = float(marker)
    except (TypeError, ValueError):
        return False
    blob = text or ""
    # Fast path: literal variants
    for v in stake_text_variants(target):
        if v in blob:
            return True
    for amt in parse_amounts(blob):
        if abs(amt - target) <= tol:
            return True
    return False


def text_has_team(text: str, team: Optional[str]) -> bool:
    """Optional team/substring match (case-insensitive). Empty team → True."""
    if team is None:
        return True
    t = str(team).strip()
    if not t:
        return True
    return t.casefold() in (text or "").casefold()


def _page_blob(page, limit: int = 12000) -> str:
    try:
        return (page.inner_text("body") or "")[:limit]
    except Exception:
        try:
            return (page.content() or "")[:limit]
        except Exception:
            return ""


def _odds_changed(page) -> bool:
    blob = _page_blob(page, 8000)
    return any(h.casefold() in blob.casefold() for h in ODDS_CHANGED_HINTS)


def _success_visible(page) -> bool:
    blob = _page_blob(page, 8000)
    return any(h.casefold() in blob.casefold() for h in SUCCESS_HINTS)


def ensure_deschise_panel(page) -> None:
    """Open PARIURILE MELE → Deschise; scroll only happens inside panel later."""
    # Prefer right-rail my-bets on home; fall back to open history URL.
    for label in ("PARIURILE MELE", "Pariurile mele", "My Bets"):
        try:
            loc = page.get_by_text(label, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3000)
                page.wait_for_timeout(400)
                break
        except Exception:
            continue
    for label in ("Deschise", "Open", "Deschis"):
        try:
            loc = page.get_by_text(label, exact=True).first
            if loc.count() == 0:
                loc = page.get_by_role("tab", name=re.compile(label, re.I)).first
            if loc.count() > 0:
                loc.click(timeout=3000)
                page.wait_for_timeout(400)
                break
        except Exception:
            continue
    # Optional Cash Out filter (skill)
    try:
        filt = page.get_by_text("Cash Out", exact=True).first
        if filt.count() > 0 and filt.is_visible():
            filt.click(timeout=2000)
            page.wait_for_timeout(300)
    except Exception:
        pass


def _card_root_for_button(btn):
    """Climb to a ticket-ish ancestor for stake/team text matching."""
    # Prefer known wrappers; fall back to a few levels up.
    for sel in (
        "xpath=ancestor::*[contains(@class,'ticket')][1]",
        "xpath=ancestor::*[contains(@class,'bet-')][1]",
        "xpath=ancestor::*[contains(@class,'card')][1]",
        "xpath=ancestor::article[1]",
        "xpath=ancestor::li[1]",
        "xpath=ancestor::div[contains(@class,'kb-')][4]",
        "xpath=ancestor::div[3]",
    ):
        try:
            root = btn.locator(sel)
            if root.count() > 0:
                return root.first
        except Exception:
            continue
    return btn


def find_cashout_button(page, stake_marker: Any, team: Optional[str] = None):
    """
    Locate the dark cashout button for the intended ticket only.
    Raises ValueError with a clear code if none / ambiguous / wrong match.
    """
    if stake_marker is None or stake_marker == "":
        if not (team and str(team).strip()):
            raise ValueError("need_marker_or_team: trimite stake_marker si/sau team")
    buttons = page.locator(CASHOUT_BUTTON_SEL)
    n = buttons.count()
    if n == 0:
        raise ValueError("no_cashout_button: niciun GTM-cashoutButton in Deschise")

    matches = []
    for i in range(n):
        btn = buttons.nth(i)
        try:
            if not btn.is_visible():
                continue
        except Exception:
            continue
        root = _card_root_for_button(btn)
        try:
            text = (root.inner_text(timeout=2000) or "") + " " + (btn.inner_text(timeout=1000) or "")
        except Exception:
            try:
                text = btn.inner_text(timeout=1000) or ""
            except Exception:
                text = ""
        stake_ok = True if stake_marker in (None, "") else text_has_stake(text, float(stake_marker))
        team_ok = text_has_team(text, team)
        if stake_ok and team_ok:
            matches.append((btn, text[:200]))

    if not matches:
        raise ValueError(
            f"ticket_not_found: stake_marker={stake_marker!r} team={team!r} "
            f"(butoane cashout vizibile: {n})"
        )
    if len(matches) > 1:
        # Never cash out the wrong ticket
        raise ValueError(
            f"ambiguous_ticket: {len(matches)} bilete potrivesc "
            f"stake_marker={stake_marker!r} team={team!r}; refineaza team"
        )
    return matches[0][0]


def cashout_on_page(
    page,
    stake_marker: Any,
    team: Optional[str] = None,
    *,
    confirm_guard_ms: int = CONFIRM_GUARD_MS_DEFAULT,
) -> dict[str, Any]:
    """
    Run skill cashout on an existing Playwright page (already on Betano, logged in).
    Uses locator.click() (CDP/real input). Does not freeze timers.
    """
    guard = int(confirm_guard_ms)
    if guard < 350:
        guard = 350
    if guard > 500:
        guard = 500

    ensure_deschise_panel(page)
    page.wait_for_timeout(200)

    btn = find_cashout_button(page, stake_marker, team)
    # First click: dark CASH OUT
    btn.scroll_into_view_if_needed(timeout=3000)
    btn.click(timeout=5000)
    t0 = time.monotonic()

    # Wait until green confirm exists (injected after first click)
    confirm = page.locator(CASHOUT_CONFIRM_SEL).first
    try:
        confirm.wait_for(state="attached", timeout=CONFIRM_WAIT_MS)
    except Exception as e:
        if _odds_changed(page):
            return {
                "ok": False,
                "error": "odds_changed",
                "message_ro": "Cotele s-au schimbat — confirmul e mort; reincarca oferta.",
            }
        return {
            "ok": False,
            "error": "confirm_missing",
            "message_ro": "Lipseste button.GTM-cashoutConfirm dupa primul click.",
            "detail": str(e)[:200],
        }

    # pointer-events guard (~350–500ms)
    page.wait_for_timeout(guard)

    if _odds_changed(page):
        return {
            "ok": False,
            "error": "odds_changed",
            "message_ro": "Cotele s-au schimbat inainte de confirm.",
        }

    # Re-query (Vue may replace the node — do not reuse stale handle)
    confirm = page.locator(CASHOUT_CONFIRM_SEL).first
    if confirm.count() == 0:
        return {
            "ok": False,
            "error": "confirm_gone",
            "message_ro": "Confirmul a disparut (TTL/~3s) inainte de click.",
        }

    elapsed_ms = (time.monotonic() - t0) * 1000
    if elapsed_ms > 2500:
        return {
            "ok": False,
            "error": "confirm_ttl",
            "message_ro": f"Prea tarziu pentru confirm ({elapsed_ms:.0f}ms > 2500ms).",
        }

    # NEVER click cancel; click green confirm center via Playwright
    cancel = page.locator(CASHOUT_CANCEL_SEL)
    # Sanity: confirm must not be the cancel button
    confirm.click(timeout=3000)

    # Success wait
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if _success_visible(page):
            return {
                "ok": True,
                "cashed_out": True,
                "stake_marker": stake_marker,
                "team": team,
                "message_ro": "Cash Out reusit (UI).",
                "confirm_elapsed_ms": round(elapsed_ms + guard, 1),
                "cancel_present_at_click": cancel.count() > 0,
            }
        if _odds_changed(page):
            return {
                "ok": False,
                "error": "odds_changed_after_confirm",
                "message_ro": "Cotele s-au schimbat dupa clickul pe confirm.",
            }
        # Confirm disappeared + no success text yet — may still be spinning
        page.wait_for_timeout(300)

    # Soft success: original button for that card gone / fewer cashout buttons
    if _success_visible(page):
        return {
            "ok": True,
            "cashed_out": True,
            "stake_marker": stake_marker,
            "team": team,
            "message_ro": "Cash Out reusit (UI).",
        }

    return {
        "ok": False,
        "error": "cashout_timeout",
        "message_ro": "Timeout: nu a aparut 'Cash Out reusit' dupa confirm.",
    }


def execute_cashout_job(job: dict) -> dict[str, Any]:
    """
    Launch persistent Betano session browser and cash out one job.
    Call only when BETANO_WORKER_EXECUTE=1 on the box (never from the admin UI process).
    """
    stake_marker = job.get("stake_marker")
    if stake_marker is None:
        stake_marker = job.get("stake")
    team = job.get("team") or job.get("teams") or job.get("team_substr")
    if isinstance(team, (list, tuple)):
        team = " ".join(str(x) for x in team[:2]) if team else None
    # selection sometimes carries a team name from place jobs
    if not team:
        team = job.get("selection") or None
        if team == "":
            team = None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return {
            "ok": False,
            "error": "playwright_missing",
            "message_ro": "Playwright nu e instalat pe box.",
            "detail": str(e)[:200],
        }

    try:
        import betano_session as bs
    except ImportError:
        try:
            from live_dashboard import betano_session as bs
        except ImportError as e:
            return {
                "ok": False,
                "error": "betano_session_missing",
                "message_ro": "betano_session indisponibil.",
                "detail": str(e)[:200],
            }

    home = getattr(bs, "HOME_URL", "https://ro.betano.com/")
    open_url = getattr(bs, "OPEN_BETS_URL", "https://ro.betano.com/myaccount/bethistory/open/")

    with sync_playwright() as p:
        context = bs._launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(home, wait_until="domcontentloaded", timeout=90000)
            if hasattr(bs, "detect_logged_in"):
                det = bs.detect_logged_in(page)
                if not det.get("logged_in"):
                    # try open-bets soft check / ensure left to caller
                    return {
                        "ok": False,
                        "error": "not_logged_in",
                        "message_ro": "Sesiune Betano expirata — ruleaza betano_session ensure.",
                        "detect": {k: det.get(k) for k in ("logged_in", "reason", "balance_hint") if k in det},
                    }
            # Prefer my-bets rail; also load open history as backup content
            try:
                ensure_deschise_panel(page)
            except Exception:
                pass
            if page.locator(CASHOUT_BUTTON_SEL).count() == 0:
                page.goto(open_url, wait_until="domcontentloaded", timeout=60000)
                ensure_deschise_panel(page)

            result = cashout_on_page(page, stake_marker, team=team)
            try:
                bs.save_storage_state(context)
            except Exception:
                pass
            return result
        finally:
            try:
                context.close()
            except Exception:
                pass
