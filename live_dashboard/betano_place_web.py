"""Betano RO place-single Playwright driver (box worker).

GATED: execute_place_job / place_on_page refuse unless BETANO_PLACE_EXECUTE=1.
Default remains enqueue-only via betano_placer — no live stakes while sleeping.

Flow when gate is open:
  1. Caller ensures betano_session / this module opens logged-in context
  2. goto match_url
  3. pick market + selection / odds
  4. set stake in betslip
  5. click PLASEAZĂ PARIU / GTM-placeBet
  6. capture stake marker hints

Does NOT cashout. Safe to import without launching a browser.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

PLACE_BUTTON_LABELS = (
    "PLASEAZĂ PARIUL",
    "PLASEAZA PARIUL",
    "PUNE PARIU",
    "PARIAZĂ",
    "PARIAZA",
    "PLACE BET",
    "Plasează pariul",
)
PLACE_BUTTON_SELS = (
    "button.GTM-placeBet",
    "button[data-qa*='place' i]",
    "button[class*='place' i]",
)
STAKE_INPUT_SELS = (
    "input[data-qa*='stake' i]",
    "input[inputmode='decimal']",
    "input[type='tel']",
    "input[type='number']",
    "input[placeholder*='Miz' i]",
    "input[class*='stake' i]",
    "[class*='betslip' i] input",
    "aside input",
)


def place_execute_enabled() -> bool:
    return str(os.environ.get("BETANO_PLACE_EXECUTE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _stake_forms(stake: float) -> tuple[str, str]:
    s = f"{float(stake):.2f}"
    return s.replace(".", ","), s


def dismiss_overlays(page) -> None:
    for label in (
        "Permite tot",
        "ACCEPTĂ",
        "Acceptă",
        "Accept all",
        "De acord",
        "OK",
        "Închide",
        "Inchide",
    ):
        try:
            b = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if b.count() and b.first.is_visible(timeout=500):
                b.first.click(timeout=2000)
                time.sleep(0.3)
                return
        except Exception:
            continue


def click_selection(
    page, *, selection: str, odds: Optional[float] = None, market: str = ""
) -> dict[str, Any]:
    detail: dict[str, Any] = {"clicked": False}
    if selection:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=re.compile(re.escape(selection), re.I))
                if loc.count():
                    loc.first.scroll_into_view_if_needed(timeout=3000)
                    loc.first.click(timeout=5000)
                    detail.update({"clicked": True, "via": f"role:{role}", "selection": selection})
                    time.sleep(1.0)
                    return detail
            except Exception:
                continue
        try:
            loc = page.get_by_text(selection, exact=False)
            if loc.count():
                loc.first.click(timeout=5000)
                detail.update({"clicked": True, "via": "text", "selection": selection})
                time.sleep(1.0)
                return detail
        except Exception:
            pass

    if odds is not None:
        try:
            variants = {f"{float(odds):.2f}", f"{float(odds):.2f}".replace(".", ",")}
            odds_btns = page.locator("button").filter(has_text=re.compile(r"^\d+[.,]\d{2}$"))
            n = odds_btns.count()
            detail["odds_buttons"] = n
            for i in range(min(n, 40)):
                t = (odds_btns.nth(i).inner_text() or "").strip()
                if t in variants:
                    odds_btns.nth(i).scroll_into_view_if_needed(timeout=3000)
                    odds_btns.nth(i).click(timeout=5000)
                    detail.update({"clicked": True, "via": "odds_value", "odds_text": t})
                    time.sleep(1.0)
                    return detail
        except Exception as exc:
            detail["odds_error"] = type(exc).__name__

    detail["error"] = "selection_not_found"
    return detail


def fill_stake(page, stake: float) -> dict[str, Any]:
    comma, dot = _stake_forms(stake)
    for sel in STAKE_INPUT_SELS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=700):
                loc.click(timeout=2000)
                try:
                    loc.fill("")
                except Exception:
                    pass
                loc.type(comma, delay=40)
                val = ""
                try:
                    val = loc.input_value()
                except Exception:
                    pass
                if not val:
                    loc.fill(dot)
                    try:
                        val = loc.input_value()
                    except Exception:
                        val = "?"
                return {"ok": True, "sel": sel, "value": val}
        except Exception:
            continue
    return {"ok": False, "error": "stake_input_not_found"}


def click_place(page) -> dict[str, Any]:
    for name in PLACE_BUTTON_LABELS:
        try:
            b = page.get_by_role("button", name=re.compile(name, re.I))
            if b.count() and b.first.is_visible(timeout=700):
                b.first.click(timeout=5000)
                return {"ok": True, "via": f"label:{name}"}
        except Exception:
            continue
    for sel in PLACE_BUTTON_SELS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=500):
                loc.click(timeout=5000)
                return {"ok": True, "via": f"sel:{sel}"}
        except Exception:
            continue
    return {"ok": False, "error": "place_button_not_found"}



def place_on_page(
    page,
    *,
    match_url: str,
    market: str = "",
    selection: str = "",
    odds: Optional[float] = None,
    stake: float,
    screenshot_dir: Optional[str] = None,
) -> dict[str, Any]:
    if not place_execute_enabled():
        return {
            "ok": False,
            "error": "place_execute_disabled",
            "skipped": True,
            "message_ro": (
                "Place Playwright gated. Set BETANO_PLACE_EXECUTE=1 to allow live place "
                "(also needs BETANO_WORKER_EXECUTE=1 on worker). Default: enqueue only."
            ),
        }

    out: dict[str, Any] = {
        "ok": False,
        "match_url": match_url,
        "market": market,
        "selection": selection,
        "odds": odds,
        "stake": stake,
        "steps": {},
    }
    shots: list[str] = []

    def _shot(name: str) -> None:
        if not screenshot_dir:
            return
        try:
            from pathlib import Path as _P

            path = _P(screenshot_dir) / f"place_{name}.png"
            page.screenshot(path=str(path), full_page=False)
            shots.append(str(path))
        except Exception:
            pass

    page.goto(match_url, wait_until="domcontentloaded", timeout=90000)
    time.sleep(1.5)
    dismiss_overlays(page)
    _shot("match")

    if market:
        try:
            mloc = page.get_by_text(market, exact=False)
            if mloc.count():
                mloc.first.click(timeout=3000)
                time.sleep(0.5)
                out["steps"]["market"] = "clicked"
        except Exception as exc:
            out["steps"]["market"] = type(exc).__name__

    sel = click_selection(page, selection=selection, odds=odds, market=market)
    out["steps"]["selection"] = sel
    if not sel.get("clicked"):
        _shot("selection_miss")
        out["error"] = sel.get("error") or "selection_not_found"
        out["screenshots"] = shots
        return out
    _shot("betslip")

    stake_r = fill_stake(page, stake)
    out["steps"]["stake"] = stake_r
    if not stake_r.get("ok"):
        _shot("stake_miss")
        out["error"] = "stake_input_not_found"
        out["screenshots"] = shots
        return out
    _shot("stake")

    place_r = click_place(page)
    out["steps"]["place_click"] = place_r
    if not place_r.get("ok"):
        _shot("place_miss")
        out["error"] = "place_button_not_found"
        out["screenshots"] = shots
        return out

    time.sleep(3.0)
    _shot("after_place")
    out["ok"] = True
    out["placed"] = True
    out["stake_marker"] = float(stake)
    out["screenshots"] = shots
    try:
        out["body_snip"] = (page.inner_text("body") or "")[:400]
    except Exception:
        pass
    return out


def execute_place_job(job: dict) -> dict[str, Any]:
    """Run a place job with Playwright. Requires BETANO_PLACE_EXECUTE=1."""
    if not place_execute_enabled():
        return {
            "ok": False,
            "error": "place_execute_disabled",
            "skipped": True,
            "message_ro": (
                "Place gated off. Export BETANO_PLACE_EXECUTE=1 (and BETANO_WORKER_EXECUTE=1) "
                "only when intentionally placing live."
            ),
        }

    match_url = str(job.get("match_url") or job.get("url") or "").strip()
    if not match_url:
        return {"ok": False, "error": "missing_match_url"}
    try:
        stake = float(job.get("stake") or job.get("stake_marker") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_stake"}
    if stake <= 0:
        return {"ok": False, "error": "bad_stake"}
    odds = job.get("odds")
    try:
        odds_f = float(odds) if odds is not None and odds != "" else None
    except (TypeError, ValueError):
        odds_f = None

    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, "live_dashboard")
    import betano_session as bs
    from playwright.sync_api import sync_playwright

    shot_dir = str(_Path(os.environ.get("BETANO_PLACE_SHOT_DIR", "state/betano")))
    with sync_playwright() as p:
        context = bs._launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(bs.HOME_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(1.0)
            dismiss_overlays(page)
            info = bs.detect_logged_in(page)
            if not info.get("logged_in"):
                return {
                    "ok": False,
                    "error": "not_logged_in",
                    "session": {
                        k: info.get(k)
                        for k in ("reason", "has_conectare", "balance_hint")
                    },
                }
            result = place_on_page(
                page,
                match_url=match_url,
                market=str(job.get("market") or ""),
                selection=str(job.get("selection") or ""),
                odds=odds_f,
                stake=stake,
                screenshot_dir=shot_dir,
            )
            try:
                bs.save_storage_state(context)
            except Exception:
                pass
            return result
        finally:
            context.close()
