"""Betano real placer — job queue for place/cashout after admin Confirm.

Default: enqueue only. A separate worker polls out/betano_jobs.jsonl.
Does NOT open Betano or spend money unless BETANO_WORKER_EXECUTE=1 (worker)
or simulate=True / BETANO_PLACER_SIMULATE=1 (tests).

Env:
  REAL_BETANO_PLACE=1     admin Confirm enqueues real place jobs
  BETANO_PLACER_SIMULATE=1  Complete jobs in-process (fake ticket ids)
  BETANO_WORKER_EXECUTE=1   Allow Playwright driver (worker only; off by default)
  BETANO_CASHOUT_EXECUTE=1  Opt-in ONLY — without this, cashout jobs fail as cashout_manual_only
                            (default: cashout is manual on phone; worker must NOT auto-cashout)
  BETANO_JOBS_DIR           Override out dir (default: live_dashboard/out)

Cashout driver notes: see betano_cashout_web.py
Driver: betano_cashout_web.py (GTM-cashoutButton → wait confirm → 350–500ms → GTM-cashoutConfirm)
Default policy: cashout = manual phone only. Set BETANO_CASHOUT_EXECUTE=1 only when user explicitly asks.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
_ENV_LOADED = False


def _load_env_files() -> None:
    """Load REAL_BETANO_* / BETANO_* from project .env without overriding shell env."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    keys_prefix = ("REAL_BETANO_", "BETANO_")
    candidates = [
        HERE.parent / ".env",
        HERE / ".env",
        HERE / "out" / ".env.local",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                if not any(k.startswith(pref) for pref in keys_prefix):
                    continue
                if k not in os.environ and v != "":
                    os.environ[k] = v
        except OSError:
            pass


def _out_dir() -> Path:
    env = os.environ.get("BETANO_JOBS_DIR")
    if env:
        p = Path(env)
    else:
        p = HERE / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_path() -> Path:
    return _out_dir() / "betano_jobs.jsonl"


def status_path() -> Path:
    return _out_dir() / "betano_job_status.json"


def place_log_path() -> Path:
    return _out_dir() / "place_log.jsonl"


def real_place_enabled() -> bool:
    _load_env_files()
    return str(os.environ.get("REAL_BETANO_PLACE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def simulate_enabled(explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    _load_env_files()
    return str(os.environ.get("BETANO_PLACER_SIMULATE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def worker_execute_enabled() -> bool:
    _load_env_files()
    return str(os.environ.get("BETANO_WORKER_EXECUTE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def cashout_execute_enabled() -> bool:
    """Live Playwright cashout — OFF by default (manual phone policy)."""
    _load_env_files()
    return str(os.environ.get("BETANO_CASHOUT_EXECUTE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )



def place_execute_enabled() -> bool:
    """Live Playwright place — OFF by default (sleep / no-live-bets policy)."""
    _load_env_files()
    return str(os.environ.get("BETANO_PLACE_EXECUTE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")


def _job_id(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_status_map() -> dict[str, dict]:
    p = status_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_status_map(data: dict[str, dict]) -> None:
    p = status_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _set_job_status(job_id: str, status: str, extra: Optional[dict] = None) -> dict:
    m = _load_status_map()
    row = dict(m.get(job_id) or {})
    row["job_id"] = job_id
    row["status"] = status
    row["updated_at"] = time.time()
    row["updated_iso"] = _now_iso()
    if extra:
        row.update(extra)
    m[job_id] = row
    # keep last 200
    if len(m) > 200:
        ordered = sorted(m.items(), key=lambda kv: float(kv[1].get("updated_at") or 0))
        m = dict(ordered[-200:])
    _save_status_map(m)
    return row


def _enqueue(job: dict) -> dict:
    job = dict(job)
    job.setdefault("ts", time.time())
    job.setdefault("ts_iso", _now_iso())
    job.setdefault("status", "queued")
    job.setdefault("book", "betano")
    _append_jsonl(jobs_path(), job)
    _set_job_status(
        job["job_id"],
        "queued",
        {
            "type": job.get("type"),
            "tip_id": job.get("tip_id"),
            "stake": job.get("stake"),
            "stake_marker": job.get("stake_marker"),
            "ticket_id": job.get("ticket_id"),
            "match_url": job.get("match_url"),
            "selection": job.get("selection"),
            "team": job.get("team"),
            "odds": job.get("odds"),
            "source": job.get("source"),
        },
    )
    return job


def _log_place(action: str, job: dict, extra: Optional[dict] = None) -> None:
    row = {
        "ts": time.time(),
        "ts_iso": _now_iso(),
        "action": action,
        "dry_run": False,
        "real_place": action in ("PLACED", "PLACE_QUEUED"),
        "job_id": job.get("job_id"),
        "tip_id": job.get("tip_id"),
        "book": "betano",
        "match_url": job.get("match_url"),
        "market": job.get("market"),
        "selection": job.get("selection"),
        "odds": job.get("odds"),
        "stake_lei": job.get("stake"),
        "stake_marker": job.get("stake_marker") or job.get("stake"),
        "ticket_id": job.get("ticket_id") or (extra or {}).get("ticket_id"),
    }
    if extra:
        row.update({k: v for k, v in extra.items() if k not in row or row.get(k) in (None, "")})
    _append_jsonl(place_log_path(), row)


def _simulate_complete(job: dict) -> dict:
    """Fake success for tests — never touches Betano."""
    jid = job["job_id"]
    _set_job_status(jid, "running", {"note": "simulate"})
    result: dict[str, Any] = {"simulated": True}
    if job.get("type") == "place":
        tid = f"SIM-{uuid.uuid4().hex[:8]}"
        result.update(
            {
                "ticket_id": tid,
                "stake_marker": float(job.get("stake") or 0),
                "balance_after": None,
                "message_ro": "Simulare place OK (fara Betano).",
            }
        )
        _set_job_status(jid, "done", {"result": result, "ticket_id": tid, "stake_marker": result["stake_marker"]})
        job2 = dict(job)
        job2["ticket_id"] = tid
        job2["stake_marker"] = result["stake_marker"]
        _log_place("PLACED", job2, {"simulated": True, "ticket_id": tid})
        try:
            from live_dashboard import admin_queue as aq

            if job.get("tip_id"):
                aq.mark_placed(
                    str(job["tip_id"]),
                    extra={
                        "real_place": True,
                        "simulated": True,
                        "betano_job_id": jid,
                        "betano_ticket_id": tid,
                        "stake_marker": result["stake_marker"],
                        "stake_lei": job.get("stake"),
                    },
                )
        except Exception:
            pass
    else:
        result.update(
            {
                "cashed_out": True,
                "stake_marker": job.get("stake_marker"),
                "ticket_id": job.get("ticket_id"),
                "message_ro": "Simulare cashout OK (fara Betano).",
            }
        )
        _set_job_status(jid, "done", {"result": result})
        _log_place("CASHOUT", job, {"simulated": True})
        try:
            from live_dashboard import admin_queue as aq

            if job.get("tip_id"):
                aq._update_status(
                    str(job["tip_id"]),
                    "placed",
                    extra={
                        "cashed_out": True,
                        "cashout_job_id": jid,
                        "cashout_at": time.time(),
                    },
                )
        except Exception:
            pass
    return {"ok": True, "job": job, "status": _load_status_map().get(jid), "simulated": True}


# ---------------------------------------------------------------------------
# Public interfaces
# ---------------------------------------------------------------------------
def place_single(
    match_url: str,
    market: str,
    selection: str,
    odds: Any,
    stake: Any,
    *,
    tip_id: Optional[str] = None,
    source: str = "api",
    simulate: Optional[bool] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Enqueue (or simulate) a Betano single place job."""
    try:
        stake_f = float(stake)
        odds_f = float(odds) if odds is not None and odds != "" else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_stake_or_odds", "message_ro": "Miza/cota invalida."}
    if stake_f <= 0:
        return {"ok": False, "error": "bad_stake", "message_ro": "Miza trebuie sa fie > 0."}
    url = str(match_url or "").strip()
    if not url:
        return {"ok": False, "error": "missing_url", "message_ro": "Lipseste match_url Betano."}

    job = {
        "job_id": _job_id("place"),
        "type": "place",
        "status": "queued",
        "tip_id": tip_id,
        "match_url": url,
        "market": str(market or ""),
        "selection": str(selection or ""),
        "odds": odds_f,
        "stake": stake_f,
        "stake_marker": stake_f,
        "ticket_id": None,
        "book": "betano",
        "source": source,
    }
    if extra:
        job["extra"] = extra
    job = _enqueue(job)
    _log_place("PLACE_QUEUED", job)

    if simulate_enabled(simulate):
        return _simulate_complete(job)
    return {
        "ok": True,
        "queued": True,
        "job_id": job["job_id"],
        "job": job,
        "message_ro": "Job place pus in coada pentru box worker.",
        "real_place_flag": real_place_enabled(),
    }



def _parse_stake_marker(value: Any) -> Optional[float]:
    """Parse stake marker accepting 2.59 or 2,59 (RO UI)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def cashout_ticket(
    stake_marker: Any = None,
    ticket_id: Optional[str] = None,
    *,
    tip_id: Optional[str] = None,
    team: Optional[str] = None,
    source: str = "api",
    simulate: Optional[bool] = None,
) -> dict:
    """Enqueue (or simulate) cashout for an open Betano ticket.

    Matching on box worker uses stake_marker (2,59 / 2.59) and optional team
    substring — see betano_cashout_web.py / skill betano-cashout-web.
    """
    marker = None
    if stake_marker is not None and stake_marker != "":
        marker = _parse_stake_marker(stake_marker)
        if marker is None:
            return {"ok": False, "error": "bad_stake_marker", "message_ro": "stake_marker invalid."}
    tid = str(ticket_id or "").strip() or None
    if marker is None and not tid and not tip_id:
        return {
            "ok": False,
            "error": "need_marker_or_id",
            "message_ro": "Trimite stake_marker, ticket_id sau tip_id.",
        }

    # Resolve from tip if only tip_id
    if tip_id and marker is None and not tid:
        try:
            from live_dashboard import admin_queue as aq

            t = aq.get_ticket(str(tip_id))
            if t:
                marker = t.get("stake_marker") or t.get("stake_lei")
                tid = tid or t.get("betano_ticket_id") or t.get("ticket_id")
                if marker is not None:
                    marker = float(marker)
        except Exception:
            pass

    team_s = str(team).strip() if team else ""
    job = {
        "job_id": _job_id("cashout"),
        "type": "cashout",
        "status": "queued",
        "tip_id": tip_id,
        "stake_marker": marker,
        "stake": marker,
        "ticket_id": tid,
        "team": team_s or None,
        "book": "betano",
        "source": source,
        "match_url": "https://ro.betano.com/myaccount/bethistory/open/",
        "market": "",
        "selection": team_s,  # reuse selection field for team hint in status UI
        "odds": None,
    }
    job = _enqueue(job)
    _log_place("CASHOUT_QUEUED", job)

    if simulate_enabled(simulate):
        return _simulate_complete(job)
    return {
        "ok": True,
        "queued": True,
        "job_id": job["job_id"],
        "job": job,
        "message_ro": "Job cashout pus in coada pentru box worker.",
    }


def status(job_id: Optional[str] = None) -> dict:
    """Return job status map or a single job."""
    m = _load_status_map()
    jobs = _read_jsonl(jobs_path())
    if job_id:
        st = m.get(job_id)
        job_row = next((j for j in reversed(jobs) if j.get("job_id") == job_id), None)
        return {
            "ok": True,
            "job_id": job_id,
            "status": st,
            "job": job_row,
            "real_place_enabled": real_place_enabled(),
            "simulate_enabled": simulate_enabled(),
            "worker_execute_enabled": worker_execute_enabled(),
        }
    recent = jobs[-30:]
    return {
        "ok": True,
        "real_place_enabled": real_place_enabled(),
        "simulate_enabled": simulate_enabled(),
        "worker_execute_enabled": worker_execute_enabled(),
        "jobs_path": str(jobs_path()),
        "status_path": str(status_path()),
        "count": len(jobs),
        "recent": recent,
        "status_map": m,
    }


def enqueue_place_from_ticket(ticket: dict, *, source: str = "admin_confirm") -> dict:
    """Build place_single args from an admin ticket dict."""
    if not isinstance(ticket, dict):
        return {"ok": False, "error": "bad_ticket"}
    tip_id = str(ticket.get("tip_id") or "")
    url = (
        ticket.get("book_url")
        or ticket.get("betano_url")
        or (ticket.get("betano") or {}).get("url")
        or ""
    )
    # Prefer Betano URL from books
    books = ticket.get("books") if isinstance(ticket.get("books"), dict) else {}
    if not url and books:
        url = (books.get("betano") or {}).get("url") or ""
    stake = ticket.get("stake_lei")
    if stake is None:
        stake = ticket.get("recommended_stake_lei") or 2.0
    return place_single(
        match_url=str(url),
        market=str(ticket.get("market") or ""),
        selection=str(ticket.get("selection") or ""),
        odds=ticket.get("odds"),
        stake=stake,
        tip_id=tip_id or None,
        source=source,
        extra={"match": ticket.get("match"), "book_pref": ticket.get("book_pref")},
    )


def list_queued(limit: int = 50) -> list[dict]:
    m = _load_status_map()
    out = []
    for job in _read_jsonl(jobs_path()):
        jid = job.get("job_id")
        st = (m.get(jid) or {}).get("status") or job.get("status")
        if st == "queued":
            out.append(job)
    return out[-limit:]


def mark_job(job_id: str, status_name: str, result: Optional[dict] = None, error: Optional[str] = None) -> dict:
    """Worker helper: update status + place_log; optionally mark admin ticket placed."""
    extra: dict[str, Any] = {}
    if result is not None:
        extra["result"] = result
    if error:
        extra["error"] = error[:500]
    row = _set_job_status(job_id, status_name, extra)
    jobs = _read_jsonl(jobs_path())
    job = next((j for j in reversed(jobs) if j.get("job_id") == job_id), {"job_id": job_id})
    if status_name == "done" and (job.get("type") == "place" or (result or {}).get("ticket_id")):
        _log_place("PLACED", job, result or {})
        tip_id = job.get("tip_id")
        if tip_id:
            try:
                from live_dashboard import admin_queue as aq

                aq.mark_placed(
                    str(tip_id),
                    extra={
                        "real_place": True,
                        "betano_job_id": job_id,
                        "betano_ticket_id": (result or {}).get("ticket_id"),
                        "stake_marker": (result or {}).get("stake_marker") or job.get("stake"),
                        "stake_lei": job.get("stake"),
                    },
                )
            except Exception:
                pass
    elif status_name == "done" and job.get("type") == "cashout":
        _log_place("CASHOUT", job, result or {})
    elif status_name == "failed":
        _log_place("PLACE_FAILED" if job.get("type") == "place" else "CASHOUT_FAILED", job, {"error": error})
        tip_id = job.get("tip_id")
        if tip_id and job.get("type") == "place":
            try:
                from live_dashboard import admin_queue as aq

                aq.mark_failed(str(tip_id), reason=error or "betano place failed")
            except Exception:
                pass
    return {"ok": True, "status": row}


def _import_cashout_web():
    """Load cashout Playwright helper (keepalive flat copy or live_dashboard package)."""
    try:
        from live_dashboard import betano_cashout_web as cw  # type: ignore
        return cw
    except ImportError:
        pass
    import importlib.util

    for path in (
        HERE / "betano_cashout_web.py",
        Path("live_dashboard/betano_cashout_web.py"),
        Path("live_dashboard/betano_cashout_web.py"),
    ):
        if path.exists():
            spec = importlib.util.spec_from_file_location("betano_cashout_web", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
    raise ImportError("betano_cashout_web not found")


def execute_job(job: dict) -> dict:
    """Run one job with Playwright when BETANO_WORKER_EXECUTE=1.

    Cashout: skipped unless BETANO_CASHOUT_EXECUTE=1 (manual phone is default).
    Place: requires BETANO_PLACE_EXECUTE=1 (default off — enqueue only).
    """
    jid = job["job_id"]
    jtype = job.get("type")
    _set_job_status(jid, "running", {"note": "execute", "type": jtype})
    if jtype == "cashout":
        if not cashout_execute_enabled():
            result = {
                "ok": False,
                "error": "cashout_manual_only",
                "skipped": True,
                "message_ro": (
                    "Cashout doar manual pe telefon. "
                    "Worker nu auto-execută cashout fără BETANO_CASHOUT_EXECUTE=1."
                ),
            }
            return mark_job(jid, "failed", result=result, error="cashout_manual_only")
        try:
            cw = _import_cashout_web()
            result = cw.execute_cashout_job(job)
        except Exception as e:
            return mark_job(jid, "failed", error=f"cashout_exception: {e}")
        if result.get("ok"):
            return mark_job(jid, "done", result=result)
        err = result.get("error") or result.get("message_ro") or "cashout_failed"
        return mark_job(jid, "failed", result=result, error=str(err))
    if jtype == "place":
        if not place_execute_enabled():
            result = {
                "ok": False,
                "error": "place_execute_disabled",
                "skipped": True,
                "message_ro": (
                    "Place Playwright gated. Set BETANO_PLACE_EXECUTE=1 "
                    "(plus BETANO_WORKER_EXECUTE=1) for live place. Default: enqueue only."
                ),
            }
            return mark_job(jid, "failed", result=result, error="place_execute_disabled")
        try:
            from betano_place_web import execute_place_job
        except ImportError:
            import importlib.util
            path = HERE / "betano_place_web.py"
            spec = importlib.util.spec_from_file_location("betano_place_web", path)
            mod = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(mod)
            execute_place_job = mod.execute_place_job
        try:
            result = execute_place_job(job)
        except Exception as e:
            return mark_job(jid, "failed", error=f"place_exception: {e}")
        if result.get("ok"):
            return mark_job(jid, "done", result=result)
        err = result.get("error") or result.get("message_ro") or "place_failed"
        return mark_job(jid, "failed", result=result, error=str(err))
    return mark_job(jid, "failed", error=f"unknown_job_type: {jtype!r}")


def poll_once(*, execute: bool = False) -> dict:
    """Pick oldest queued job. Without execute/simulate: report only.
    With simulate env: complete fake. With execute: place stub; cashout needs BETANO_CASHOUT_EXECUTE=1 else cashout_manual_only.
    """
    queued = list_queued(limit=100)
    if not queued:
        return {"ok": True, "processed": 0, "message": "no queued jobs"}
    job = queued[0]
    if simulate_enabled():
        return _simulate_complete(job)
    if execute or worker_execute_enabled():
        return execute_job(job)
    return {
        "ok": True,
        "processed": 0,
        "pending": len(queued),
        "next_job_id": job.get("job_id"),
        "message": "queued job waiting for box worker (BETANO_WORKER_EXECUTE or simulate)",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Betano placer job queue")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    p_poll = sub.add_parser("poll")
    p_poll.add_argument("--once", action="store_true", default=True)
    p_poll.add_argument("--loop", action="store_true")
    p_poll.add_argument("--interval", type=float, default=15.0)
    p_poll.add_argument("--execute", action="store_true", help="Run Playwright driver (cashout needs CASHOUT_EXECUTE; place needs PLACE_EXECUTE)")
    p_place = sub.add_parser("place")
    p_place.add_argument("--url", required=True)
    p_place.add_argument("--market", default="")
    p_place.add_argument("--selection", required=True)
    p_place.add_argument("--odds", type=float, required=True)
    p_place.add_argument("--stake", type=float, required=True)
    p_place.add_argument("--tip-id", default="")
    p_place.add_argument("--simulate", action="store_true")
    p_co = sub.add_parser("cashout")
    p_co.add_argument("--stake-marker", type=float, default=None)
    p_co.add_argument("--ticket-id", default="")
    p_co.add_argument("--tip-id", default="")
    p_co.add_argument("--team", default="", help="Optional team substring (e.g. Portugalia)")
    p_co.add_argument("--simulate", action="store_true")
    args = ap.parse_args()

    if args.cmd == "status" or args.cmd is None:
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "place":
        r = place_single(
            args.url,
            args.market,
            args.selection,
            args.odds,
            args.stake,
            tip_id=args.tip_id or None,
            simulate=True if args.simulate else None,
            source="cli",
        )
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if args.cmd == "cashout":
        r = cashout_ticket(
            stake_marker=args.stake_marker,
            ticket_id=args.ticket_id or None,
            tip_id=args.tip_id or None,
            team=args.team or None,
            simulate=True if args.simulate else None,
            source="cli",
        )
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if args.cmd == "poll":
        if args.loop:
            while True:
                print(json.dumps(poll_once(execute=args.execute), ensure_ascii=False))
                time.sleep(max(2.0, float(args.interval)))
        print(json.dumps(poll_once(execute=args.execute), indent=2, ensure_ascii=False))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
