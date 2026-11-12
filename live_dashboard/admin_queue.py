"""Admin ticket queue: pending -> confirmed/skipped; confirm appends place_queue.jsonl."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ADMIN_QUEUE = OUT / "admin_queue.jsonl"
PLACE_QUEUE = OUT / "place_queue.jsonl"
PLACE_LOG = OUT / "place_log.jsonl"
ADMIN_HALT = OUT / "admin_halt.json"
BETANO_OPEN = OUT / "betano_open.json"
BETANO_SETTLED = OUT / "betano_settled.json"
BETANO_OPEN_URL = "https://ro.betano.com/myaccount/bethistory/open"
BETANO_SETTLED_URL = "https://ro.betano.com/myaccount/bethistory/"
OUT.mkdir(parents=True, exist_ok=True)

STATUSES = ("pending", "confirmed", "skipped", "placed_dry", "placed", "failed", "expired")
BANK_LEI_REF = 100.0
DEST_CHANNELS = ("whatsapp", "discord", "telegram")
BUCHAREST = timezone(timedelta(hours=3))
TAB_IDS = ("va_urma", "in_desfasurare", "plasate", "stabilite", "expirate")

STATUS_LABEL_RO = {
    "pending": "In asteptare",
    "confirmed": "Aprobat",
    "placed_dry": "Plasat (dry)",
    "placed": "Plasat",
    "skipped": "Skip",
    "failed": "Esuat",
    "expired": "Expirat",
}


def status_label_ro(status: str) -> str:
    return STATUS_LABEL_RO.get(str(status or ""), str(status or "—"))


def _append(path: Path, row: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _rewrite(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _potential_return(odds: Any, stake: Any) -> Optional[float]:
    try:
        o = float(odds)
        s = float(stake)
        if o > 1 and s > 0:
            return round(o * s, 2)
    except (TypeError, ValueError):
        pass
    return None


def _potential_profit(odds: Any, stake: Any) -> Optional[float]:
    ret = _potential_return(odds, stake)
    if ret is None:
        return None
    try:
        return round(ret - float(stake), 2)
    except (TypeError, ValueError):
        return None


def get_halt() -> dict:
    try:
        d = json.loads(ADMIN_HALT.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"halted": False}
    except Exception:
        return {"halted": False, "reason": None, "until_date": None}


def set_halt(halted: bool, reason: str = "") -> dict:
    today = datetime.now(BUCHAREST).strftime("%Y-%m-%d")
    payload = {
        "halted": bool(halted),
        "reason": (reason or "").strip() or ("Oprit manual din admin" if halted else ""),
        "until_date": today if halted else None,
        "updated_at": time.time(),
        "updated_iso": datetime.now(BUCHAREST).isoformat(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    ADMIN_HALT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def is_halted_today() -> bool:
    h = get_halt()
    if not h.get("halted"):
        return False
    today = datetime.now(BUCHAREST).strftime("%Y-%m-%d")
    until = h.get("until_date")
    if until and str(until) < today:
        set_halt(False, reason="expired_halt_day")
        return False
    return True


def _parse_start(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        # ISO
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return None


def expire_stale_pending(now: Optional[float] = None) -> int:
    """Mark pending tickets past match start as expired. Returns count."""
    now = time.time() if now is None else now
    rows = _read_jsonl(ADMIN_QUEUE)
    changed = 0
    new_rows = []
    for r in rows:
        r2 = dict(r)
        if r2.get("status") == "pending":
            st = _parse_start(r2.get("start"))
            if st is not None and st < now:
                r2["status"] = "expired"
                r2["fail_reason"] = "Neaprobat inainte de start meci"
                r2["updated_at"] = now
                changed += 1
        new_rows.append(r2)
    if changed:
        _rewrite(ADMIN_QUEUE, new_rows)
    return changed


def _pick_book_pref(tip: dict, book: str, book_key: str) -> str:
    """Prefer Superbet when odds close vs Betano; else prefer Betano if only one."""
    books = tip.get("books") if isinstance(tip.get("books"), dict) else {}
    betano_odds = None
    superbet_odds = None
    if isinstance(tip.get("betano"), dict) and tip["betano"].get("odds") is not None:
        betano_odds = tip["betano"].get("odds")
    if isinstance(tip.get("superbet"), dict) and tip["superbet"].get("odds") is not None:
        superbet_odds = tip["superbet"].get("odds")
    if books:
        betano_odds = betano_odds if betano_odds is not None else (books.get("betano") or {}).get("odds")
        superbet_odds = superbet_odds if superbet_odds is not None else (books.get("superbet") or {}).get("odds")
    pref = tip.get("book_pref") or tip.get("preferred_book")
    if pref:
        return str(pref).lower()
    try:
        if betano_odds is not None and superbet_odds is not None:
            b, s = float(betano_odds), float(superbet_odds)
            # close = within 3% -> Superbet by default; else better odds
            if abs(b - s) / max(b, s) <= 0.03:
                return "superbet"
            return "superbet" if s >= b else "betano"
    except (TypeError, ValueError):
        pass
    if "superbet" in book_key:
        return "superbet"
    if "betano" in book_key:
        return "betano"
    return "betano"


def tip_to_card(tip: dict, status: str = "pending") -> dict:
    tip = dict(tip or {})
    tip_id = str(tip.get("tip_id") or tip.get("id") or "")
    odds = tip.get("odds")
    stake = tip.get("stake_lei") if tip.get("stake_lei") is not None else tip.get("stake")
    book = tip.get("book") or tip.get("book_key") or ""
    book_key = str(tip.get("book_key") or book or "").lower()
    book_url = tip.get("book_url") or tip.get("url") or ""
    betano = tip.get("betano") or {}
    if isinstance(betano, dict):
        if betano.get("odds") is not None:
            odds = betano.get("odds")
        if betano.get("url"):
            book_url = betano.get("url")
            book = "Betano"
            book_key = "betano"
    if "betano" in book_key:
        book = tip.get("book") or "Betano"
    why = tip.get("why_ro") or tip.get("ollama_why") or tip.get("why") or ""
    reasons = tip.get("reasons") or tip.get("present") or []
    edge = tip.get("edge_pct") if tip.get("edge_pct") is not None else tip.get("edge")
    ev = tip.get("ev_pct") if tip.get("ev_pct") is not None else tip.get("ev")
    risk = tip.get("risk_level") or tip.get("risk") or ""
    start = tip.get("start") or tip.get("start_time") or tip.get("ts")
    recommended_stake = stake if stake is not None else 2.0
    books_avail = {}
    if isinstance(tip.get("betano"), dict) and tip["betano"].get("odds") is not None:
        books_avail["betano"] = {"odds": tip["betano"].get("odds"), "url": tip["betano"].get("url")}
    if isinstance(tip.get("superbet"), dict) and tip["superbet"].get("odds") is not None:
        books_avail["superbet"] = {"odds": tip["superbet"].get("odds"), "url": tip["superbet"].get("url")}
    if isinstance(tip.get("books"), dict):
        books_avail.update(tip["books"])

    card = {
        "tip_id": tip_id,
        "match": tip.get("match") or "",
        "match_id": tip.get("match_id") or "",
        "teams": tip.get("teams") or [],
        "selection": tip.get("selection") or "",
        "market": tip.get("market") or "Match Winner",
        "book": book,
        "book_key": book_key,
        "book_url": book_url,
        "book_pref": _pick_book_pref(tip, book, book_key),
        "books_avail": books_avail,
        "odds": odds,
        "odds_at_queue": odds,
        "stake_lei": stake,
        "recommended_stake_lei": recommended_stake,
        "potential_return_lei": _potential_return(odds, stake),
        "potential_profit_lei": _potential_profit(odds, stake),
        "edge_pct": edge,
        "ev_pct": ev,
        "risk": risk,
        "why": why,
        "reasons": reasons,
        "start": start,
        "status": status,
        "status_label_ro": status_label_ro(status),
        "sport": tip.get("sport") or "CS2",
        "league": tip.get("league"),
        "disclaimer": tip.get("disclaimer"),
        "queued_at": tip.get("queued_at") or time.time(),
        "updated_at": tip.get("updated_at") or time.time(),
        "destinations": tip.get("destinations") or [],
        "send_to_group": bool(tip.get("send_to_group") or False),
        "personal_note": tip.get("personal_note") or "",
        "fail_reason": tip.get("fail_reason") or "",
        "ticket_type": tip.get("ticket_type") or ("multi" if tip.get("legs") else "single"),
        "legs": tip.get("legs") or [],
        "source_tip": tip,
    }
    try:
        from live_dashboard.multi_ticket import enrich_ticket_multi
        card = enrich_ticket_multi(card)
        card["status_label_ro"] = status_label_ro(card.get("status") or status)
    except Exception:
        pass
    return card


def enqueue_from_tip(tip: dict, force: bool = False) -> Optional[dict]:
    card = tip_to_card(tip, status="pending")
    if not card["tip_id"]:
        from live_dashboard.tip_store import make_id
        card["tip_id"] = make_id(tip)
        tip = dict(tip)
        tip["tip_id"] = card["tip_id"]
        card = tip_to_card(tip, status="pending")
    existing = list_tickets(expire=False)
    for row in existing:
        if row.get("tip_id") == card["tip_id"]:
            if force and row.get("status") == "pending":
                return row
            return None if not force else row
    _append(ADMIN_QUEUE, card)
    return card


def sync_pending_from_store(limit: int = 40) -> int:
    added = 0
    try:
        from live_dashboard.tip_store import list_tips
        for tip in list_tips(limit=limit):
            if enqueue_from_tip(tip):
                added += 1
    except Exception:
        pass
    bilete = OUT / "bilete.jsonl"
    for row in _read_jsonl(bilete)[-limit:]:
        if row.get("gate") and row.get("gate") != "approved":
            continue
        if not row.get("tip_id"):
            try:
                from live_dashboard.tip_store import make_id, save_tip
                tid = make_id(row)
                row = dict(row)
                row["tip_id"] = tid
                save_tip(row)
            except Exception:
                continue
        if enqueue_from_tip(row):
            added += 1
    return added


def list_tickets(status: Optional[str] = None, limit: int = 300, expire: bool = True) -> list[dict]:
    if expire:
        expire_stale_pending()
    rows = _read_jsonl(ADMIN_QUEUE)
    by_id: dict[str, dict] = {}
    for r in rows:
        tid = str(r.get("tip_id") or "")
        if not tid:
            continue
        by_id[tid] = r
    out = list(by_id.values())
    # enrich labels / multi on read
    enriched = []
    for r in out:
        r = dict(r)
        r["status_label_ro"] = status_label_ro(r.get("status"))
        try:
            from live_dashboard.multi_ticket import enrich_ticket_multi
            if r.get("legs") or r.get("ticket_type") == "multi":
                r = enrich_ticket_multi(r)
        except Exception:
            pass
        enriched.append(r)
    out = enriched
    if status:
        out = [r for r in out if r.get("status") == status]
    out.sort(key=lambda r: float(r.get("queued_at") or r.get("updated_at") or 0), reverse=True)
    return out[:limit]


def get_ticket(tip_id: str) -> Optional[dict]:
    for r in list_tickets(limit=500, expire=True):
        if r.get("tip_id") == tip_id:
            return r
    return None


def _update_status(tip_id: str, status: str, extra: Optional[dict] = None) -> Optional[dict]:
    rows = _read_jsonl(ADMIN_QUEUE)
    found = None
    new_rows = []
    for r in rows:
        if r.get("tip_id") == tip_id:
            r = dict(r)
            r["status"] = status
            r["status_label_ro"] = status_label_ro(status)
            r["updated_at"] = time.time()
            if extra:
                r.update(extra)
            found = r
        new_rows.append(r)
    if not found:
        return None
    _rewrite(ADMIN_QUEUE, new_rows)
    return found


def _normalize_destinations(destinations: Any) -> list[str]:
    if not destinations:
        return []
    if isinstance(destinations, str):
        destinations = [destinations]
    out = []
    for d in destinations:
        d = str(d or "").strip().lower()
        if d in DEST_CHANNELS and d not in out:
            out.append(d)
    return out


def odds_recheck(ticket: dict, live_odds: Optional[float] = None) -> dict:
    """Compare queued odds vs optional live/override; warn if moved badly against us."""
    queued = ticket.get("odds_at_queue")
    if queued is None:
        queued = ticket.get("odds")
    try:
        q = float(queued) if queued is not None else None
    except (TypeError, ValueError):
        q = None
    live = live_odds
    if live is None:
        # soft: use books_avail preferred
        pref = (ticket.get("book_pref") or "betano").lower()
        ba = ticket.get("books_avail") or {}
        if isinstance(ba.get(pref), dict) and ba[pref].get("odds") is not None:
            live = ba[pref].get("odds")
        elif ticket.get("odds") is not None:
            live = ticket.get("odds")
    try:
        l = float(live) if live is not None else None
    except (TypeError, ValueError):
        l = None
    warn = False
    moved_pct = None
    msg = "Cota neschimbata / fara feed live — foloseste cota din coada."
    if q and l:
        moved_pct = round((l - q) / q * 100.0, 2)
        # bad for backer if odds shortened a lot
        if l < q * 0.94:  # >6% shorter
            warn = True
            msg = f"Atentie: cota a scazut {abs(moved_pct):.1f}% ({q} → {l}). Edge posibil erodat."
        elif l > q * 1.04:
            msg = f"Cota a crescut {moved_pct:.1f}% ({q} → {l}) — posibil mai bine."
        else:
            msg = f"Cota aproximativ stabila ({q} → {l})."
    return {
        "queued_odds": q,
        "live_odds": l,
        "moved_pct": moved_pct,
        "warn": warn,
        "message_ro": msg,
    }


def today_history() -> dict:
    """W/L/pending + rough P&L from today's queue/log (dry-run aware)."""
    today = datetime.now(BUCHAREST).strftime("%Y-%m-%d")
    tickets = list_tickets(limit=200, expire=True)
    pending = won = lost = skipped = approved = placed = failed = expired = 0
    pnl = 0.0
    for t in tickets:
        # filter to today by queued_at / updated
        ts = float(t.get("updated_at") or t.get("queued_at") or 0)
        day = datetime.fromtimestamp(ts, BUCHAREST).strftime("%Y-%m-%d") if ts else ""
        if day != today and t.get("status") != "pending":
            # still count pending always
            if t.get("status") != "pending":
                continue
        st = t.get("status")
        if st == "pending":
            pending += 1
        elif st == "skipped":
            skipped += 1
        elif st == "confirmed":
            approved += 1
        elif st == "placed_dry":
            placed += 1
            # dry P&L unknown — use expected profit as soft marker 0
        elif st == "failed":
            failed += 1
        elif st == "expired":
            expired += 1
        # settlement fields if present
        if t.get("result") in ("W", "win", "won"):
            won += 1
            try:
                pnl += float(t.get("pnl_lei") or t.get("potential_profit_lei") or 0)
            except (TypeError, ValueError):
                pass
        elif t.get("result") in ("L", "loss", "lost"):
            lost += 1
            try:
                pnl -= float(t.get("stake_lei") or 0)
            except (TypeError, ValueError):
                pass
    return {
        "date": today,
        "pending": pending,
        "won": won,
        "lost": lost,
        "skipped": skipped,
        "approved": approved,
        "placed": placed,
        "failed": failed,
        "expired": expired,
        "pnl_lei": round(pnl, 2),
        "bank_lei": BANK_LEI_REF,
    }


def confirm_ticket(
    tip_id: str,
    stake_lei: Optional[float] = None,
    send_to_group: bool = False,
    destinations: Optional[list] = None,
    personal_note: str = "",
    book_pref: Optional[str] = None,
    live_odds: Optional[float] = None,
) -> dict:
    if is_halted_today():
        h = get_halt()
        return {
            "ok": False,
            "error": "halted",
            "message_ro": f"Kill switch activ: {h.get('reason') or 'oprit azi'}.",
        }
    ticket = get_ticket(tip_id)
    if not ticket:
        return {"ok": False, "error": "not_found", "message_ro": "Bilet inexistent in coada admin."}
    if ticket.get("status") == "confirmed":
        return {"ok": True, "ticket": ticket, "already": True}
    if ticket.get("status") in ("skipped", "placed_dry", "expired", "failed"):
        return {
            "ok": False,
            "error": "bad_status",
            "message_ro": f"Status curent: {ticket.get('status')} — nu se poate confirma.",
            "ticket": ticket,
        }

    # Multi guard: allow approve of NU e OK only with explicit note (still dry-run)
    if ticket.get("ticket_type") == "multi" and ticket.get("verdict_ok") is False:
        # still allow but flag
        pass

    dests = _normalize_destinations(destinations)
    # legacy: send_to_group true → whatsapp
    if send_to_group and "whatsapp" not in dests:
        dests.append("whatsapp")
    send_to_group = len(dests) > 0

    recheck = odds_recheck(ticket, live_odds=live_odds)

    extra: dict[str, Any] = {
        "send_to_group": bool(send_to_group),
        "destinations": dests,
        "personal_note": (personal_note or "")[:500],
        "odds_recheck": recheck,
    }
    if book_pref:
        bp = str(book_pref).lower().strip()
        if bp in ("betano", "superbet"):
            extra["book_pref"] = bp
    if stake_lei is not None:
        try:
            stake_val = float(stake_lei)
            if stake_val <= 0:
                return {"ok": False, "error": "bad_stake", "message_ro": "Miza trebuie sa fie > 0."}
            odds = ticket.get("odds")
            if recheck.get("live_odds"):
                odds = recheck["live_odds"]
            extra["stake_lei"] = stake_val
            extra["stake_override"] = True
            extra["potential_return_lei"] = _potential_return(odds, stake_val)
            extra["potential_profit_lei"] = _potential_profit(odds, stake_val)
            # bank pct
            extra["stake_bank_pct"] = round(stake_val / BANK_LEI_REF * 100.0, 2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_stake", "message_ro": "Miza invalida."}

    updated = _update_status(tip_id, "confirmed", extra=extra)
    book_for_place = (updated or {}).get("book_pref") or "betano"
    real_place = False
    try:
        from live_dashboard.betano_placer import real_place_enabled
        real_place = bool(real_place_enabled()) and str(book_for_place).lower() == "betano"
    except Exception:
        real_place = False
    payload = {
        "queued_at": time.time(),
        "action": "place_real" if real_place else "place_dry",
        "book": book_for_place,
        "status": "queued",
        "tip_id": tip_id,
        "ticket": updated,
        "stake_lei": (updated or {}).get("stake_lei"),
        "send_to_group": bool(send_to_group),
        "destinations": dests,
        "personal_note": (personal_note or "")[:500],
        "odds_recheck": recheck,
        "placer_notes": {
            "dry_run_only": not real_place,
            "never_click_place_bet": not real_place,
            "real_betano_place": real_place,
            "otp_source": "email",
            "otp_hint": "OTP by email or SMS via /admin/otp.",
            "send_to_group": bool(send_to_group),
            "destinations": dests,
            "personal_notify_stub": "Notificare personala pending = viitor (nu in UI v1).",
        },
    }
    betano_job = None
    if real_place:
        try:
            from live_dashboard import betano_placer as bp
            betano_job = bp.enqueue_place_from_ticket(updated or {}, source="admin_confirm")
            if isinstance(betano_job, dict) and betano_job.get("job_id"):
                payload["betano_job_id"] = betano_job["job_id"]
                _update_status(tip_id, "confirmed", extra={
                    "betano_job_id": betano_job["job_id"],
                    "real_place_pending": True,
                    "stake_marker": (updated or {}).get("stake_lei"),
                })
                updated = get_ticket(tip_id) or updated
                payload["ticket"] = updated
        except Exception as e:
            payload["betano_enqueue_error"] = f"{type(e).__name__}: {e}"[:300]
    _append(PLACE_QUEUE, payload)
    return {
        "ok": True,
        "ticket": updated,
        "place_queue": str(PLACE_QUEUE),
        "send_to_group": bool(send_to_group),
        "destinations": dests,
        "odds_recheck": recheck,
        "real_place": real_place,
        "betano_job": (betano_job or {}).get("job") if isinstance(betano_job, dict) else betano_job,
        "betano_job_id": (betano_job or {}).get("job_id") if isinstance(betano_job, dict) else None,
    }


def skip_ticket(tip_id: str) -> dict:
    ticket = get_ticket(tip_id)
    if not ticket:
        return {"ok": False, "error": "not_found", "message_ro": "Bilet inexistent in coada admin."}
    if ticket.get("status") == "skipped":
        return {"ok": True, "ticket": ticket, "already": True}
    if ticket.get("status") == "placed_dry":
        return {
            "ok": False,
            "error": "bad_status",
            "message_ro": "Deja marked placed_dry — nu se poate skip.",
            "ticket": ticket,
        }
    updated = _update_status(tip_id, "skipped")
    return {"ok": True, "ticket": updated}


def mark_placed_dry(tip_id: str) -> Optional[dict]:
    return _update_status(tip_id, "placed_dry")


def mark_placed(tip_id: str, extra: Optional[dict] = None) -> Optional[dict]:
    """Mark ticket as really placed on Betano."""
    payload = {"real_place": True}
    if extra:
        payload.update(extra)
    return _update_status(tip_id, "placed", extra=payload)


def mark_failed(tip_id: str, reason: str = "") -> Optional[dict]:
    return _update_status(tip_id, "failed", extra={"fail_reason": (reason or "")[:300]})


# —— Tabs / Betano open / bulk skip (UI v2) ——

def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_num(d: dict, *keys):
    for k in keys:
        if k in d and d.get(k) is not None and d.get(k) != "":
            return _num(d.get(k))
    return None


def _norm_betano_open_bet(b: dict) -> dict:
    stake = _first_num(b, "stake_lei", "stake", "amount")
    pot = _first_num(b, "potential_return_lei", "potential_return", "returns", "to_return")
    cashout = _first_num(b, "cashout_lei", "cashout", "cash_out")
    return {
        "id": str(b.get("id") or b.get("bet_id") or b.get("ticket_id") or ""),
        "type": b.get("type") or b.get("bet_type") or "",
        "match": b.get("match") or b.get("event") or b.get("name") or "—",
        "selection": b.get("selection") or b.get("pick") or b.get("legs_summary") or "—",
        "market": b.get("market") or b.get("raw_note") or "",
        "odds": _first_num(b, "odds", "total_odds"),
        "stake_lei": stake,
        "potential_return_lei": pot,
        "cashout_lei": cashout,
        "status": b.get("status") or "open",
        "start": b.get("start") or b.get("start_time") or b.get("event_time"),
        "placed_at": b.get("placed_at") or b.get("created_at"),
        "url": b.get("url") or BETANO_OPEN_URL,
        "raw_note": b.get("note") or b.get("market") or "",
    }


def _norm_betano_settled_bet(b: dict) -> dict:
    stake = _first_num(b, "stake_lei", "stake", "amount")
    ret = _first_num(b, "return_lei", "returns", "return", "payout", "actual_return")
    pnl = _first_num(b, "pnl_lei", "pnl", "profit")
    if pnl is None and stake is not None and ret is not None:
        pnl = round(ret - stake, 2)
    status_raw = str(b.get("status") or b.get("result") or "").lower()
    if status_raw in ("w", "won", "castigat", "câștigat", "win", "settled_won"):
        result = "won"
    elif status_raw in ("l", "lost", "pierdut", "lose", "settled_lost"):
        result = "lost"
    elif status_raw in ("void", "anulat", "push", "refund"):
        result = "void"
    elif pnl is not None:
        result = "won" if pnl > 0 else ("lost" if pnl < 0 else "void")
    else:
        result = status_raw or "settled"
    label = {"won": "Câștigat", "lost": "Pierdut", "void": "Anulat"}.get(result, result or "Settled")
    return {
        "id": str(b.get("id") or b.get("bet_id") or b.get("ticket_id") or ""),
        "type": b.get("type") or b.get("bet_type") or "",
        "match": b.get("match") or b.get("event") or b.get("name") or "—",
        "selection": b.get("selection") or b.get("pick") or b.get("legs_summary") or "—",
        "market": b.get("market") or "",
        "odds": _first_num(b, "odds", "total_odds"),
        "stake_lei": stake,
        "return_lei": ret,
        "pnl_lei": pnl,
        "result": result,
        "result_label_ro": label,
        "status": b.get("status") or result,
        "settled_at": b.get("settled_at") or b.get("resolved_at") or b.get("closed_at"),
        "start": b.get("start") or b.get("start_time") or b.get("event_time"),
        "url": b.get("url") or BETANO_SETTLED_URL,
        "raw_note": b.get("note") or b.get("market") or "",
    }


def load_betano_open() -> dict:
    """Read out/betano_open.json if present (filled by scraper/session sync)."""
    empty = {
        "ok": False,
        "source": "none",
        "updated_at": None,
        "synced_at": None,
        "balance_lei": None,
        "open_count": 0,
        "url": BETANO_OPEN_URL,
        "bets": [],
        "count": 0,
        "note": None,
        "message_ro": "Niciun export local. Deschide istoricul pe Betano sau așteaptă sync.",
    }
    path = BETANO_OPEN
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        out = dict(empty)
        out["ok"] = False
        out["source"] = "error"
        out["message_ro"] = f"betano_open.json invalid: {e}"
        return out
    if not isinstance(raw, dict):
        out = dict(empty)
        out["message_ro"] = "betano_open.json trebuie să fie un obiect JSON."
        return out
    bets = raw.get("bets") or raw.get("open_bets") or raw.get("items") or []
    if not isinstance(bets, list):
        bets = []
    norm = [_norm_betano_open_bet(b) for b in bets if isinstance(b, dict)]
    updated = raw.get("synced_at") or raw.get("updated_at") or raw.get("ts") or raw.get("scraped_at")
    balance = _num(raw.get("balance_lei") if raw.get("balance_lei") is not None else raw.get("balance"))
    declared = raw.get("open_count")
    try:
        declared_n = int(declared) if declared is not None else len(norm)
    except (TypeError, ValueError):
        declared_n = len(norm)
    msg = raw.get("message_ro")
    if not msg:
        if norm:
            extra = f" (din {declared_n} pe Betano)" if declared_n > len(norm) else ""
            msg = f"{len(norm)} pariuri deschise pe Betano{extra}"
        else:
            msg = "Export gol — niciun open bet."
    return {
        "ok": True,
        "source": str(raw.get("source") or "betano_open.json"),
        "updated_at": updated,
        "synced_at": updated,
        "balance_lei": balance,
        "open_count": declared_n,
        "url": str(raw.get("url") or BETANO_OPEN_URL),
        "bets": norm,
        "count": len(norm),
        "note": raw.get("note"),
        "message_ro": msg,
    }


def load_betano_settled() -> dict:
    """Read out/betano_settled.json (Stabilite) if present."""
    empty = {
        "ok": False,
        "source": "none",
        "updated_at": None,
        "synced_at": None,
        "settled_count": 0,
        "url": BETANO_SETTLED_URL,
        "bets": [],
        "count": 0,
        "won": 0,
        "lost": 0,
        "pnl_lei": 0.0,
        "note": None,
        "message_ro": "Niciun export Stabilite. Așteaptă scrape din istoricul Betano.",
    }
    path = BETANO_SETTLED
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        out = dict(empty)
        out["source"] = "error"
        out["message_ro"] = f"betano_settled.json invalid: {e}"
        return out
    if not isinstance(raw, dict):
        out = dict(empty)
        out["message_ro"] = "betano_settled.json trebuie să fie un obiect JSON."
        return out
    bets = raw.get("bets") or raw.get("settled_bets") or raw.get("items") or []
    if not isinstance(bets, list):
        bets = []
    norm = [_norm_betano_settled_bet(b) for b in bets if isinstance(b, dict)]
    updated = raw.get("synced_at") or raw.get("updated_at") or raw.get("ts") or raw.get("scraped_at")
    declared = raw.get("settled_count")
    try:
        declared_n = int(declared) if declared is not None else len(norm)
    except (TypeError, ValueError):
        declared_n = len(norm)
    won = sum(1 for b in norm if b.get("result") == "won")
    lost = sum(1 for b in norm if b.get("result") == "lost")
    pnl_vals = [b.get("pnl_lei") for b in norm if b.get("pnl_lei") is not None]
    pnl_total = round(sum(pnl_vals), 2) if pnl_vals else 0.0
    msg = raw.get("message_ro")
    if not msg:
        if norm:
            msg = f"{len(norm)} bilete stabilite pe Betano (W{won}/L{lost})"
        elif declared_n:
            msg = f"Stabilite: {declared_n} pe Betano — lista locală încă goală (scrape pending)."
        else:
            msg = "Export gol — niciun bilet stabilit."
    return {
        "ok": True,
        "source": str(raw.get("source") or "betano_settled.json"),
        "updated_at": updated,
        "synced_at": updated,
        "settled_count": declared_n,
        "url": str(raw.get("url") or BETANO_SETTLED_URL),
        "bets": norm,
        "count": len(norm),
        "won": won,
        "lost": lost,
        "pnl_lei": pnl_total,
        "note": raw.get("note"),
        "message_ro": msg,
    }


def _ticket_from_place_row(row: dict, status_fallback: str = "confirmed") -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    ticket = row.get("ticket") if isinstance(row.get("ticket"), dict) else None
    tip_id = str(row.get("tip_id") or (ticket or {}).get("tip_id") or "")
    if not tip_id and ticket:
        tip_id = str(ticket.get("id") or "")
    if not tip_id:
        return None
    base = dict(ticket) if ticket else {}
    for k, v in row.items():
        if k in ("ticket",):
            continue
        if k not in base or base.get(k) in (None, ""):
            base[k] = v
    base["tip_id"] = tip_id
    st = str(row.get("status") or base.get("status") or status_fallback)
    if st in ("queued", "would_place", "WOULD_PLACE", "pending_place"):
        st = "confirmed"
    if st == "WOULD_PLACE" or row.get("action") == "WOULD_PLACE":
        st = "placed_dry"
    if row.get("action") in ("PLACED", "PLACE_QUEUED") or row.get("real_place"):
        if row.get("action") == "PLACED":
            st = "placed"
            base["real_place"] = True
        if row.get("stake_marker") is not None:
            base["stake_marker"] = row.get("stake_marker")
        if row.get("ticket_id"):
            base["betano_ticket_id"] = row.get("ticket_id")
        if row.get("job_id"):
            base["betano_job_id"] = row.get("job_id")
    if st not in STATUSES:
        st = status_fallback
    base["status"] = st
    base["status_label_ro"] = status_label_ro(st)
    if base.get("match") is None:
        base["match"] = row.get("match") or "—"
    if base.get("selection") is None:
        base["selection"] = row.get("selection") or "—"
    if "queued_at" not in base:
        base["queued_at"] = row.get("queued_at") or row.get("ts") or time.time()
    base["from_place_queue"] = True
    return base


def merge_placed_sources(existing: list[dict]) -> list[dict]:
    """Ensure approved/placed from place_queue + place_log appear even if admin_queue lagged."""
    by_id: dict[str, dict] = {}
    for t in existing:
        tid = str(t.get("tip_id") or "")
        if tid:
            by_id[tid] = dict(t)
    for path, fallback in ((PLACE_QUEUE, "confirmed"), (PLACE_LOG, "placed_dry")):
        for row in _read_jsonl(path):
            card = _ticket_from_place_row(row, status_fallback=fallback)
            if not card:
                continue
            tid = str(card.get("tip_id") or "")
            if not tid:
                continue
            cur = by_id.get(tid)
            if not cur:
                by_id[tid] = card
                continue
            # upgrade status toward placed
            rank = {"pending": 0, "skipped": 1, "expired": 1, "failed": 2, "confirmed": 3, "placed_dry": 4, "placed": 5}
            if rank.get(str(card.get("status")), 0) > rank.get(str(cur.get("status")), 0):
                merged = dict(cur)
                merged.update({k: v for k, v in card.items() if v not in (None, "")})
                merged["status"] = card["status"]
                merged["status_label_ro"] = status_label_ro(card["status"])
                by_id[tid] = merged
    out = list(by_id.values())
    out.sort(key=lambda r: float(r.get("queued_at") or r.get("updated_at") or 0), reverse=True)
    return out


def classify_tab(ticket: dict, now: Optional[float] = None) -> str:
    """Mutually exclusive tab id for a local admin ticket."""
    now = time.time() if now is None else now
    st = str(ticket.get("status") or "")
    start_ts = _parse_start(ticket.get("start"))

    if st in ("expired", "skipped", "failed"):
        return "expirate"
    if st in ("confirmed", "placed_dry", "placed"):
        if start_ts is not None and start_ts <= now:
            return "in_desfasurare"
        return "plasate"
    # pending
    if start_ts is not None and start_ts <= now:
        return "in_desfasurare"
    return "va_urma"


def classify_tickets(tickets: list[dict], now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    buckets = {k: [] for k in TAB_IDS}
    for t in tickets:
        t = dict(t)
        tab = classify_tab(t, now=now)
        t["tab"] = tab
        buckets[tab].append(t)
    counts = {k: len(buckets[k]) for k in TAB_IDS}
    return {"buckets": buckets, "counts": counts, "now": now}


def skip_all_expired() -> dict:
    """Skip is a no-op for already-expired; mark any stale pending past start as expired, return count."""
    n = expire_stale_pending()
    rows = _read_jsonl(ADMIN_QUEUE)
    expired_ids = [r.get("tip_id") for r in rows if r.get("status") == "expired"]
    # Also convert pending past start already handled; optionally skip expired duplicates — nothing else
    return {
        "ok": True,
        "expired_marked": n,
        "expired_count": len([x for x in expired_ids if x]),
        "message_ro": f"Actualizat: {n} marcate expirate. Total expirate: {len([x for x in expired_ids if x])}.",
    }
