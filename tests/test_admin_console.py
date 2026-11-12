"""Minimal test: simulate pending tickets, confirm/skip, dry-run placer, stake+destinations."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from fastapi.testclient import TestClient
    from live_dashboard import admin_auth, admin_queue as aq
    from live_dashboard.dry_run_placer import process_queue
    from live_dashboard.multi_ticket import simulate_multi
    from live_dashboard.main import app

    tmp = Path(tempfile.mkdtemp(prefix="admin_console_test_"))
    aq.OUT = tmp
    aq.ADMIN_QUEUE = tmp / "admin_queue.jsonl"
    aq.PLACE_QUEUE = tmp / "place_queue.jsonl"
    aq.PLACE_LOG = tmp / "place_log.jsonl"
    aq.ADMIN_HALT = tmp / "admin_halt.json"
    admin_auth.OUT = tmp
    admin_auth.TOKEN_FILE = tmp / "admin_token.txt"
    token = admin_auth.get_or_create_admin_token()

    tip1 = {
        "tip_id": "testtip001a",
        "match": "Alpha vs Beta",
        "selection": "Alpha",
        "market": "Match Winner",
        "book": "Betano",
        "book_key": "betano",
        "book_url": "https://www.betano.ro/example/1",
        "odds": 1.85,
        "stake_lei": 2.0,
        "edge_pct": 5.5,
        "ev_pct": 4.2,
        "risk_level": "mediu",
        "why_ro": "Test edge pozitiv pe Alpha.",
        "reasons": ["cs2", "intl_ref", "quality"],
        "start": "2099-09-24T20:00:00+03:00",
        "ts": "2026-09-24T10:00:00+03:00",
        "upset_rate": 0.10,
    }
    tip2 = {
        "tip_id": "testtip002b",
        "match": "Gamma vs Delta",
        "selection": "Delta",
        "market": "Match Winner",
        "book": "Superbet",
        "book_key": "superbet",
        "book_url": "https://superbet.ro/example/2",
        "odds": 2.10,
        "stake_lei": 2.0,
        "edge_pct": 3.0,
        "ev_pct": 2.5,
        "risk_level": "ridicat",
        "why_ro": "Test skip candidate.",
        "reasons": ["cs2"],
        "ts": "2026-09-24T10:01:00+03:00",
        "start": "2099-09-24T21:00:00+03:00",
    }
    assert aq.enqueue_from_tip(tip1) is not None
    assert aq.enqueue_from_tip(tip2) is not None
    pending = aq.list_tickets(status="pending")
    assert len(pending) >= 2, pending

    client = TestClient(app)

    r = client.get("/admin/tickets")
    assert r.status_code == 401, r.status_code

    r = client.get("/admin/tickets", params={"a": token})
    assert r.status_code == 200, r.text[:300]
    assert "Alpha vs Beta" in r.text
    assert "Supreme Admin" in r.text
    assert "island" in r.text or "pending" in r.text.lower()

    # Confirm with stake override + destinations default empty (personal-only)
    r = client.post(
        f"/api/admin/confirm/testtip001a?a={token}",
        json={"stake_lei": 5.0, "destinations": [], "send_to_group": False, "personal_note": "doar eu", "book_pref": "betano"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok"), body
    assert body.get("destinations") == []
    assert body.get("send_to_group") is False
    assert (tmp / "place_queue.jsonl").exists()
    pq_line = (tmp / "place_queue.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    pq = json.loads(pq_line)
    assert pq.get("stake_lei") == 5.0
    assert pq.get("destinations") == []
    assert pq.get("send_to_group") is False
    assert (pq.get("ticket") or {}).get("stake_lei") == 5.0
    assert "doar eu" in str(pq.get("personal_note") or "")

    # Default destinations off when omitted
    tip3 = dict(tip1)
    tip3["tip_id"] = "testtip003c"
    tip3["match"] = "Echo vs Foxtrot"
    assert aq.enqueue_from_tip(tip3) is not None
    r = client.post(f"/api/admin/confirm/testtip003c?a={token}", json={"stake_lei": 2.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("destinations") == []
    assert body.get("send_to_group") is False

    r = client.post(f"/api/admin/skip/testtip002b?a={token}")
    assert r.status_code == 200, r.text
    assert r.json().get("ok")
    t2 = aq.get_ticket("testtip002b")
    assert t2 and t2.get("status") == "skipped", t2

    # Kill switch blocks confirm
    aq.set_halt(True, reason="test halt")
    tip4 = dict(tip1)
    tip4["tip_id"] = "testtip004d"
    tip4["match"] = "Halt vs Test"
    assert aq.enqueue_from_tip(tip4) is not None
    r = client.post(f"/api/admin/confirm/testtip004d?a={token}", json={"stake_lei": 2.0})
    assert r.status_code == 423, r.text
    aq.set_halt(False)

    # Multi sim: juice combined should be NU e OK; fantasy NU e OK
    sim_juice = simulate_multi([
        {"match": "A vs B", "selection": "A", "odds": 1.20, "model_prob": 0.80, "upset_rate": 0.10},
        {"match": "C vs D", "selection": "C", "odds": 1.25, "model_prob": 0.78, "upset_rate": 0.10},
    ], stake_lei=2)
    assert sim_juice["verdict_ok"] is False
    assert sim_juice["recomandat"] is False

    sim_fantasy = simulate_multi([
        {"match": "A vs B", "selection": "A", "odds": 2.2, "model_prob": 0.42, "upset_rate": 0.12},
        {"match": "C vs D", "selection": "C", "odds": 2.3, "model_prob": 0.40, "upset_rate": 0.12},
        {"match": "E vs F", "selection": "E", "odds": 2.2, "model_prob": 0.42, "upset_rate": 0.12},
    ], stake_lei=2)
    assert sim_fantasy["combined_odds"] > 10
    assert sim_fantasy["recomandat"] is False

    # Upset-prone leg blocks safe multi
    sim_upset = simulate_multi([
        {"match": "A vs B", "selection": "A", "odds": 1.55, "model_prob": 0.62, "upset_rate": 0.32},
        {"match": "C vs D", "selection": "C", "odds": 1.60, "model_prob": 0.58, "upset_rate": 0.10},
    ], stake_lei=2)
    assert sim_upset["verdict_ok"] is False
    assert "upset_prone_leg" in sim_upset["flags"]

    import live_dashboard.dry_run_placer as dr
    dr.OUT = tmp
    dr.PLACE_QUEUE = tmp / "place_queue.jsonl"
    dr.PLACE_LOG = tmp / "place_log.jsonl"
    dr.STATE = tmp / "dry_run_placer_state.json"
    logged = process_queue()
    assert logged, "expected WOULD_PLACE entries"
    assert logged[0]["action"] == "WOULD_PLACE"
    t1 = aq.get_ticket("testtip001a")
    assert t1 and t1.get("status") == "placed_dry", t1

    card = aq.tip_to_card(tip1)
    assert card["potential_return_lei"] == round(1.85 * 2.0, 2)

    print("PASS test_admin_console")
    print("tmp=", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
