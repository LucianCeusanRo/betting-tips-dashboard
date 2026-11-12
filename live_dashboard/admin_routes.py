"""Admin console routes: /admin — list pending tickets, Confirm / Skip.

Auth: ADMIN token via ?a= / cookie sp_admin / header X-Admin-Token.
Separate from public tip token (access_gate).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
TEMPLATES.mkdir(exist_ok=True)

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES))


def _admin_token(request: Request, a: Optional[str] = None) -> Optional[str]:
    return (
        a
        or request.cookies.get("sp_admin")
        or request.headers.get("x-admin-token")
        or request.query_params.get("a")
    )


def _gate(request: Request, a: Optional[str] = None) -> dict:
    from live_dashboard.admin_auth import check_admin
    return check_admin(_admin_token(request, a))


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request):
    """Always show login form; if already authed, bounce to /admin."""
    if _gate(request).get("ok"):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        request, "admin_login.html",
        {"request": request, "error": None, "username": ""},
    )


@router.post("/admin/login", response_class=HTMLResponse)
async def admin_login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    from live_dashboard.admin_auth import check_admin_password

    result = check_admin_password(username, password)
    if not result.get("ok"):
        return templates.TemplateResponse(
            request, "admin_login.html",
            {
                "request": request,
                "error": result.get("message_ro") or "User sau parolă greșită.",
                "username": username or "",
            },
            status_code=401,
        )
    tok = result["token"]
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        "sp_admin",
        tok,
        max_age=14 * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@router.post("/admin/logout")
@router.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie("sp_admin", path="/")
    return resp


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/tickets", response_class=HTMLResponse)
async def admin_console(request: Request, a: Optional[str] = Query(None), sync: int = Query(0)):
    from live_dashboard import admin_queue as aq

    gate = _gate(request, a)
    if not gate.get("ok"):
        # Fancy login page instead of serving locked console HTML
        resp = templates.TemplateResponse(
            request, "admin_login.html",
            {
                "request": request,
                "error": None if not a else gate.get("message_ro"),
                "username": "",
            },
            status_code=401,
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    if sync:
        aq.sync_pending_from_store()
    else:
        if not aq.list_tickets(limit=1):
            aq.sync_pending_from_store()

    tickets = aq.merge_placed_sources(aq.list_tickets(limit=300))
    classified = aq.classify_tickets(tickets)
    buckets = classified["buckets"]
    tab_counts = classified["counts"]
    # Flatten for template: annotate tab; order = va_urma, in_desfasurare, plasate, expirate
    ordered = []
    for key in aq.TAB_IDS:
        ordered.extend(buckets.get(key) or [])
    pending = buckets.get("va_urma") or []
    pending_count = len(pending) + len(buckets.get("in_desfasurare") or [])
    # pending_count for island: only true pending statuses
    pending_count = len([t for t in tickets if t.get("status") == "pending"])
    history = aq.today_history()
    halt = aq.get_halt()
    halted = aq.is_halted_today()
    betano_open = aq.load_betano_open()
    betano_settled = aq.load_betano_settled()
    # Surface Betano reality counts on tabs (local + Betano)
    tab_counts = dict(tab_counts or {})
    local_plasate = int(tab_counts.get("plasate") or 0)
    betano_open_n = max(int(betano_open.get("count") or 0), int(betano_open.get("open_count") or 0))
    tab_counts["plasate"] = max(local_plasate, betano_open_n)
    tab_counts["stabilite"] = int(betano_settled.get("count") or 0) or int(betano_settled.get("settled_count") or 0)
    betano_balance = betano_open.get("balance_lei")

    resp = templates.TemplateResponse(
        request, "admin_console.html",
        {
            "request": request,
            "error": None,
            "tickets": ordered,
            "tab_counts": tab_counts,
            "betano_open": betano_open,
            "betano_settled": betano_settled,
            "betano_open_url": aq.BETANO_OPEN_URL,
            "betano_settled_url": aq.BETANO_SETTLED_URL,
            "betano_balance_lei": betano_balance,
            "default_tab": "va_urma",
            "token": a or request.cookies.get("sp_admin") or "",
            "authed": True,
            "pending_count": pending_count,
            "bank_lei": aq.BANK_LEI_REF,
            "history": history,
            "halt": halt,
            "halted": halted,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    tok = _admin_token(request, a)
    if tok:
        resp.set_cookie("sp_admin", tok, max_age=14 * 86400, httponly=True, samesite="lax")
    return resp


@router.get("/api/admin/tickets")
async def api_list(request: Request, a: Optional[str] = Query(None), status: Optional[str] = None):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse(
            {"ok": False, "error": gate.get("message_ro")},
            status_code=int(gate.get("code") or 401),
        )
    from live_dashboard import admin_queue as aq
    aq.sync_pending_from_store()
    tickets = aq.merge_placed_sources(aq.list_tickets(status=status, limit=300))
    classified = aq.classify_tickets(tickets)
    bo = aq.load_betano_open()
    bs = aq.load_betano_settled()
    counts = dict(classified["counts"] or {})
    counts["plasate"] = max(int(counts.get("plasate") or 0), int(bo.get("count") or 0), int(bo.get("open_count") or 0))
    counts["stabilite"] = int(bs.get("count") or 0) or int(bs.get("settled_count") or 0)
    return {
        "ok": True,
        "tickets": tickets,
        "count": len(tickets),
        "tab_counts": counts,
        "betano_open": bo,
        "betano_settled": bs,
        "betano_balance_lei": bo.get("balance_lei"),
        "history": aq.today_history(),
        "halted": aq.is_halted_today(),
        "bank_lei": aq.BANK_LEI_REF,
    }


@router.post("/api/admin/confirm/{tip_id}")
async def api_confirm(tip_id: str, request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse(
            {"ok": False, "error": gate.get("message_ro")},
            status_code=int(gate.get("code") or 401),
        )
    from live_dashboard import admin_queue as aq
    body = await _json_body(request)
    stake = body.get("stake_lei", body.get("stake"))
    stake_f = None
    if stake is not None and stake != "":
        try:
            stake_f = float(stake)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "bad_stake", "message_ro": "Miza invalida."},
                status_code=400,
            )
    dests = body.get("destinations")
    if dests is None and body.get("send_to_group"):
        dests = ["whatsapp"]
    live_odds = body.get("live_odds")
    try:
        live_odds_f = float(live_odds) if live_odds is not None else None
    except (TypeError, ValueError):
        live_odds_f = None

    result = aq.confirm_ticket(
        tip_id,
        stake_lei=stake_f,
        send_to_group=bool(body.get("send_to_group") or False),
        destinations=dests if isinstance(dests, list) else None,
        personal_note=str(body.get("personal_note") or body.get("note") or ""),
        book_pref=body.get("book_pref"),
        live_odds=live_odds_f,
    )
    code = 200 if result.get("ok") else (404 if result.get("error") == "not_found" else 400)
    if result.get("error") == "halted":
        code = 423
    return JSONResponse(result, status_code=code)


@router.post("/api/admin/skip/{tip_id}")
async def api_skip(tip_id: str, request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse(
            {"ok": False, "error": gate.get("message_ro")},
            status_code=int(gate.get("code") or 401),
        )
    from live_dashboard import admin_queue as aq
    result = aq.skip_ticket(tip_id)
    code = 200 if result.get("ok") else (404 if result.get("error") == "not_found" else 400)
    return JSONResponse(result, status_code=code)


@router.post("/api/admin/recheck/{tip_id}")
async def api_recheck(tip_id: str, request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    from live_dashboard import admin_queue as aq
    ticket = aq.get_ticket(tip_id)
    if not ticket:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    body = await _json_body(request)
    live = body.get("live_odds")
    try:
        live_f = float(live) if live is not None else None
    except (TypeError, ValueError):
        live_f = None
    return {"ok": True, "recheck": aq.odds_recheck(ticket, live_odds=live_f), "ticket": ticket}


@router.get("/api/admin/halt")
@router.post("/api/admin/halt")
async def api_halt(request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    from live_dashboard import admin_queue as aq
    if request.method == "GET":
        return {"ok": True, "halt": aq.get_halt(), "halted": aq.is_halted_today()}
    body = await _json_body(request)
    halted = body.get("halted")
    if halted is None:
        halted = not aq.is_halted_today()
    payload = aq.set_halt(bool(halted), reason=str(body.get("reason") or ""))
    return {"ok": True, "halt": payload, "halted": aq.is_halted_today()}


@router.get("/api/admin/history")
async def api_history(request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    from live_dashboard import admin_queue as aq
    return {"ok": True, "history": aq.today_history()}


@router.post("/api/admin/skip_expired")
async def api_skip_expired(request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    from live_dashboard import admin_queue as aq
    return aq.skip_all_expired()


@router.post("/admin/confirm/{tip_id}")
async def form_confirm(tip_id: str, request: Request, a: Optional[str] = Form(None)):
    tok = a or _admin_token(request)
    gate = _gate(request, tok)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    from live_dashboard import admin_queue as aq
    aq.confirm_ticket(tip_id)
    url = "/admin/tickets"
    if tok:
        url += f"?a={tok}"
    return RedirectResponse(url, status_code=303)


@router.post("/admin/skip/{tip_id}")
async def form_skip(tip_id: str, request: Request, a: Optional[str] = Form(None)):
    tok = a or _admin_token(request)
    gate = _gate(request, tok)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    from live_dashboard import admin_queue as aq
    aq.skip_ticket(tip_id)
    url = "/admin/tickets"
    if tok:
        url += f"?a={tok}"
    return RedirectResponse(url, status_code=303)


@router.get("/admin/otp", response_class=HTMLResponse)
async def admin_otp_page(request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return HTMLResponse(
            "<h1>Neautorizat</h1><p>Deschide /admin/otp?a=TOKEN</p>",
            status_code=401,
        )
    from live_dashboard.otp_inbox import SMS_PHONE_DISPLAY
    tok = _admin_token(request, a) or ""
    html = f"""<!DOCTYPE html><html lang="ro"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>OTP Betano — Admin</title>
<style>body{{font-family:-apple-system,system-ui;background:#070b12;color:#edf4ff;padding:24px;padding-top:calc(24px + env(safe-area-inset-top))}}
.card{{background:#111c2b;padding:20px;border-radius:20px;max-width:420px;border:1px solid #223044}}
input,button,select{{font-size:1.1rem;padding:12px;border-radius:12px;border:0;margin:6px 0;width:100%}}
button{{background:#31e6a1;color:#06120e;font-weight:800;cursor:pointer;min-height:48px}}
.meta{{color:#8290a4;font-size:.9rem}}</style></head><body>
<div class="card">
<h1>OTP Betano</h1>
<p class="meta">SMS: <b>{SMS_PHONE_DISPLAY}</b>. Email: automat.</p>
<p class="meta">Nu lipi codul in chat — doar aici.</p>
<form method="post" action="/admin/otp">
<input type="hidden" name="a" value="{tok}"/>
<label class="meta">Canal</label>
<select name="channel">
<option value="sms">SMS</option>
<option value="email">Email (manual)</option>
</select>
<input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="Cod OTP" required/>
<button type="submit">Salveaza OTP</button>
</form>
<p class="meta"><a href="/admin/tickets" style="color:#4bc7ff">Inapoi la bilete</a></p>
</div></body></html>"""
    resp = HTMLResponse(html)
    if tok:
        resp.set_cookie("sp_admin", tok, max_age=14 * 86400, httponly=True, samesite="lax")
    return resp


@router.post("/admin/otp")
async def admin_otp_submit(
    request: Request,
    code: str = Form(...),
    channel: str = Form("sms"),
    a: Optional[str] = Form(None),
):
    tok = a or _admin_token(request)
    gate = _gate(request, tok)
    if not gate.get("ok"):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    from live_dashboard.otp_inbox import append_otp
    result = append_otp(code, channel=channel)
    if not result.get("ok"):
        return HTMLResponse(f"<h1>Eroare</h1><p>{result.get('message_ro')}</p>", status_code=400)
    return HTMLResponse(
        "<h1>OTP salvat</h1><p>Cod stocat local pentru placer (viitor). "
        "<a href='/admin/tickets'>Inapoi</a></p>"
    )


@router.post("/api/admin/otp")
async def api_otp(request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    body = await _json_body(request)
    from live_dashboard.otp_inbox import append_otp
    return append_otp(str(body.get("code") or ""), channel=str(body.get("channel") or "sms"))


@router.get("/api/admin/betano/status")
async def api_betano_status(request: Request, a: Optional[str] = Query(None), job_id: Optional[str] = None):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False, "error": gate.get("message_ro")}, status_code=int(gate.get("code") or 401))
    from live_dashboard import betano_placer as bp
    return bp.status(job_id)


@router.post("/api/admin/betano/cashout")
async def api_betano_cashout(request: Request, a: Optional[str] = Query(None)):
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False, "error": gate.get("message_ro")}, status_code=int(gate.get("code") or 401))
    body = await _json_body(request)
    from live_dashboard import betano_placer as bp
    result = bp.cashout_ticket(
        stake_marker=body.get("stake_marker", body.get("stake")),
        ticket_id=body.get("ticket_id") or body.get("betano_ticket_id"),
        tip_id=body.get("tip_id"),
        source="admin_api",
        simulate=body.get("simulate"),
    )
    code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=code)


@router.post("/api/admin/betano/place/{tip_id}")
async def api_betano_place(tip_id: str, request: Request, a: Optional[str] = Query(None)):
    """Re-enqueue a place job for a confirmed/placed tip (Betano only)."""
    gate = _gate(request, a)
    if not gate.get("ok"):
        return JSONResponse({"ok": False, "error": gate.get("message_ro")}, status_code=int(gate.get("code") or 401))
    from live_dashboard import admin_queue as aq
    from live_dashboard import betano_placer as bp
    ticket = aq.get_ticket(tip_id)
    if not ticket:
        return JSONResponse({"ok": False, "error": "not_found", "message_ro": "Bilet inexistent."}, status_code=404)
    body = await _json_body(request)
    result = bp.enqueue_place_from_ticket(ticket, source="admin_requeue")
    if body.get("simulate") and result.get("queued") and result.get("job"):
        result = bp._simulate_complete(result["job"])
    code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=code)
