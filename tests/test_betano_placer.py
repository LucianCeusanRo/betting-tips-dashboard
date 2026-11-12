"""Tests for Betano placer job queue — simulated only, no live bets."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from fastapi.testclient import TestClient
    from live_dashboard import admin_auth, admin_queue as aq
    from live_dashboard import betano_placer as bp
    from live_dashboard.main import app

    tmp = Path(tempfile.mkdtemp(prefix="betano_placer_test_"))
    aq.OUT = tmp
    aq.ADMIN_QUEUE = tmp / "admin_queue.jsonl"
    aq.PLACE_QUEUE = tmp / "place_queue.jsonl"
    aq.PLACE_LOG = tmp / "place_log.jsonl"
    aq.ADMIN_HALT = tmp / "admin_halt.json"
    aq.BETANO_OPEN = tmp / "betano_open.json"
    aq.BETANO_SETTLED = tmp / "betano_settled.json"
    admin_auth.OUT = tmp
    admin_auth.TOKEN_FILE = tmp / "admin_token.txt"

    # Redirect placer out into tmp
    os.environ["BETANO_JOBS_DIR"] = str(tmp)
    os.environ.pop("REAL_BETANO_PLACE", None)
    os.environ.pop("BETANO_PLACER_SIMULATE", None)
    os.environ.pop("BETANO_WORKER_EXECUTE", None)

    token = admin_auth.get_or_create_admin_token()

    # --- unit: place_single enqueue ---
    r = bp.place_single(
        "https://ro.betano.com/odds/example/1/",
        "Match Winner",
        "Bounty Hunters",
        1.32,
        2.58,
        tip_id="simtip001",
        source="test",
        simulate=False,
    )
    assert r.get("ok") and r.get("queued"), r
    jid = r["job_id"]
    assert (tmp / "betano_jobs.jsonl").exists()
    st = bp.status(jid)
    assert st["status"]["status"] == "queued", st

    # --- unit: simulate place ---
    r2 = bp.place_single(
        "https://ro.betano.com/odds/example/2/",
        "Match Winner",
        "Alpha",
        1.85,
        5.0,
        tip_id="simtip002",
        simulate=True,
    )
    assert r2.get("ok") and r2.get("simulated"), r2
    assert r2["status"]["status"] == "done"
    assert (r2["status"].get("result") or {}).get("ticket_id", "").startswith("SIM-")

    # --- unit: cashout by stake marker (simulate) ---
    r3 = bp.cashout_ticket(stake_marker=2.58, tip_id="simtip001", simulate=True)
    assert r3.get("ok") and r3.get("simulated"), r3
    assert r3["status"]["status"] == "done"

    # --- unit: cashout with team hint (simulate) + stake text matchers ---
    from live_dashboard import betano_cashout_web as cw

    assert cw.text_has_stake("Miza 2,59 lei · Portugalia", 2.59)
    assert cw.text_has_stake("CASH OUT 2.59", 2.59)
    assert not cw.text_has_stake("Miza 2,58 lei", 2.59)
    assert cw.text_has_team("Portugalia vs Spania", "Portugalia")
    assert cw.text_has_team("x", None) is True
    assert "2,59" in cw.stake_text_variants(2.59)
    r3b = bp.cashout_ticket(
        stake_marker=2.59, tip_id="simtip001", team="Portugalia", simulate=True
    )
    assert r3b.get("ok") and r3b.get("simulated"), r3b
    assert r3b["job"]["team"] == "Portugalia"

    # --- Confirm without REAL flag: no betano job from confirm path ---
    tip = {
        "tip_id": "realtip001",
        "match": "BH vs Test",
        "selection": "Bounty Hunters",
        "market": "Match Winner",
        "book": "Betano",
        "book_key": "betano",
        "book_url": "https://ro.betano.com/odds/example/bh/",
        "odds": 1.32,
        "stake_lei": 2.58,
        "start": "2099-09-24T22:00:00+03:00",
        "ts": "2026-09-24T11:00:00+03:00",
    }
    assert aq.enqueue_from_tip(tip) is not None
    before = len(bp._read_jsonl(bp.jobs_path()))
    conf = aq.confirm_ticket("realtip001", stake_lei=2.58, destinations=[])
    assert conf.get("ok"), conf
    after = len(bp._read_jsonl(bp.jobs_path()))
    assert after == before, "dry Confirm must not enqueue real job"
    assert conf.get("real_place") in (False, None) or conf.get("betano_job") is None

    # --- Confirm with REAL_BETANO_PLACE=1 ---
    os.environ["REAL_BETANO_PLACE"] = "1"
    tip2 = dict(tip)
    tip2["tip_id"] = "realtip002"
    tip2["match"] = "Gamma vs Delta"
    assert aq.enqueue_from_tip(tip2) is not None
    conf2 = aq.confirm_ticket("realtip002", stake_lei=2.58, destinations=[], book_pref="betano")
    assert conf2.get("ok"), conf2
    assert conf2.get("real_place") is True, conf2
    assert conf2.get("betano_job", {}).get("job_id"), conf2
    job = conf2["betano_job"]
    assert job["type"] == "place"
    assert float(job["stake"]) == 2.58
    assert "bh" in (job.get("match_url") or "").lower() or "example" in (job.get("match_url") or "")

    # place_queue should mark place_real
    pq = json.loads((tmp / "place_queue.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert pq.get("action") in ("place_real", "place_dry")  # place_real expected
    assert pq.get("action") == "place_real", pq.get("action")
    assert (pq.get("placer_notes") or {}).get("dry_run_only") is False

    # --- API status / cashout ---
    client = TestClient(app)
    r = client.get("/api/admin/betano/status", params={"a": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok")
    assert body.get("real_place_enabled") is True

    r = client.post(
        "/api/admin/betano/cashout",
        params={"a": token},
        json={"stake_marker": 2.58, "tip_id": "realtip002"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") and body.get("queued"), body

    # UI hint: admin page should mention real / cashout when flag on
    r = client.get("/admin/tickets", params={"a": token})
    assert r.status_code == 200
    assert "Cashout" in r.text or "cashout" in r.text.lower()
    assert "REAL" in r.text or "real" in r.text.lower() or "Betano REAL" in r.text

    # cleanup env
    os.environ.pop("REAL_BETANO_PLACE", None)
    os.environ.pop("BETANO_JOBS_DIR", None)
    print("test_betano_placer: OK", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
