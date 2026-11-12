"""Dry-run placer: consume place_queue.jsonl and log WOULD_PLACE.

This worker never places a real bet. When REAL_BETANO_PLACE is enabled,
place_real rows are left for betano_placer instead of being marked placed_dry.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PLACE_QUEUE = OUT / "place_queue.jsonl"
PLACE_LOG = OUT / "place_log.jsonl"
STATE = OUT / "dry_run_placer_state.json"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_state() -> dict:
    if not STATE.exists():
        return {"done_ids": []}
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"done_ids": []}
        data.setdefault("done_ids", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"done_ids": []}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def process_queue() -> list[dict]:
    """Process queued dry-run place jobs. Returns the WOULD_PLACE rows written this run."""
    state = _load_state()
    done_ids = {str(x) for x in state.get("done_ids") or []}
    rows = _read_jsonl(PLACE_QUEUE)
    logged: list[dict] = []

    # When REAL_BETANO_PLACE=1, place_real rows are owned by betano_placer / box worker.
    try:
        from live_dashboard.betano_placer import real_place_enabled

        _real_on = bool(real_place_enabled())
    except Exception:
        _real_on = False

    for row in rows:
        tip_id = str((row.get("tip_id") or (row.get("ticket") or {}).get("tip_id") or ""))
        if not tip_id or tip_id in done_ids:
            continue
        if row.get("action") == "place_real" or (_real_on and row.get("action") != "place_dry"):
            # Leave for betano_placer worker; do not mark placed_dry
            continue

        ticket = row.get("ticket") if isinstance(row.get("ticket"), dict) else {}
        entry = {
            "ts": time.time(),
            "action": "WOULD_PLACE",
            "tip_id": tip_id,
            "book": row.get("book") or ticket.get("book"),
            "stake_lei": row.get("stake_lei") or ticket.get("stake_lei"),
            "match": row.get("match") or ticket.get("match"),
            "selection": row.get("selection") or ticket.get("selection"),
            "odds": row.get("odds") or ticket.get("odds"),
            "ticket": ticket,
            "dry_run": True,
        }
        _append(PLACE_LOG, entry)
        from live_dashboard import admin_queue as aq

        aq.mark_placed_dry(tip_id)
        done_ids.add(tip_id)
        logged.append(entry)

    state["done_ids"] = sorted(done_ids)
    state["updated_at"] = time.time()
    _save_state(state)
    return logged


if __name__ == "__main__":
    written = process_queue()
    print(f"WOULD_PLACE {len(written)}")
