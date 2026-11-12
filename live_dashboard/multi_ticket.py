"""Multi (accumulator) tickets: 2–3 legs max, worth-risking odds, upset-aware sim.

Singles remain primary. Multis are opt-in personal admin tickets.
Philosophy: if analysis is good, surprises aren't surprises — bake historical
upset/surprise rate into joint prob + verdict. Never mark recomandat if not OK.
Dry-run only — no real placement here.
"""
from __future__ import annotations

import random
from typing import Any, Optional

MAX_LEGS = 3
MIN_LEGS = 2
# Worth-risking combined targets (not juice, not fantasy)
MIN_COMBINED_ODDS_2 = 2.20
MAX_COMBINED_ODDS_2 = 6.50
MIN_COMBINED_ODDS_3 = 3.00
MAX_COMBINED_ODDS_3 = 12.0
MAX_LEG_ODDS = 2.35
MIN_LEG_ODDS = 1.28  # below = tiny juice, not worth multi risk
MIN_EV_OK = 0.0  # OK requires non-negative EV after upset haircut
SIM_N = 5000

# Upset flags
UPSET_HIGH = 0.28  # recent favorite-loss / upset rate
UPSET_MED = 0.18
VOL_HIGH = 0.35


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def upset_profile(team_or_leg: dict) -> dict:
    """Build surprise/upset profile from tip fields or nested stats.

    Expected optional fields (any source):
      upset_rate, favorite_loss_rate, surprise_rate, volatility,
      form_volatility, upset_note
    """
    src = dict(team_or_leg or {})
    nested = src.get("upset_profile") if isinstance(src.get("upset_profile"), dict) else {}
    team = src.get("team_stats") if isinstance(src.get("team_stats"), dict) else {}

    def pick(*keys):
        for k in keys:
            for bag in (src, nested, team):
                if bag.get(k) is not None:
                    return _f(bag.get(k))
        return None

    upset = pick("upset_rate", "surprise_rate", "favorite_loss_rate", "upset_as_fav_rate")
    vol = pick("volatility", "form_volatility", "vol")
    # If missing, infer soft prior from odds: shorter odds claimed "safe" get small prior risk
    odds = _f(src.get("odds"))
    if upset is None:
        # neutral prior — unknown ≠ safe
        upset = 0.16 if odds and odds < 1.55 else 0.12
        inferred = True
    else:
        inferred = False
    if vol is None:
        vol = min(0.45, 0.10 + (upset or 0.12))
    level = "low"
    if (upset or 0) >= UPSET_HIGH or (vol or 0) >= VOL_HIGH:
        level = "high"
    elif (upset or 0) >= UPSET_MED:
        level = "med"
    note = (
        src.get("upset_note")
        or nested.get("note")
        or (
            f"Rata surpriza/upset ~{(upset or 0)*100:.0f}% (vol {(vol or 0)*100:.0f}%)"
            + (" — prior estimat" if inferred else "")
        )
    )
    return {
        "upset_rate": round(upset or 0, 3),
        "volatility": round(vol or 0, 3),
        "level": level,  # low|med|high
        "inferred": inferred,
        "note_ro": note,
        "yellow_flag": level in ("med", "high"),
        "block_safe_multi": level == "high",
    }


def _leg_prob(leg: dict) -> float:
    for k in ("model_prob", "true_prob", "prob", "p"):
        if leg.get(k) is not None:
            p = _f(leg.get(k))
            if p > 1.5:
                p = p / 100.0
            if 0 < p < 1:
                return p
    odds = _f(leg.get("odds"))
    if odds > 1.01:
        implied = 1.0 / odds
        return min(0.92, max(0.08, implied * 1.03))
    return 0.0


def normalize_legs(legs: list) -> list[dict]:
    out = []
    for i, raw in enumerate(legs or []):
        if not isinstance(raw, dict):
            continue
        odds = _f(raw.get("odds"))
        base_p = _leg_prob(raw)
        up = upset_profile(raw)
        # Haircut model prob by upset rate (surprises baked in)
        # effective_p = p * (1 - 0.65*upset) — partial credit to historical surprise
        haircut = 1.0 - 0.65 * float(up["upset_rate"])
        eff_p = max(0.05, min(0.95, base_p * haircut))
        leg = {
            "idx": i + 1,
            "match": raw.get("match") or raw.get("event") or "",
            "market": raw.get("market") or "Match Winner",
            "selection": raw.get("selection") or raw.get("pick") or "",
            "team": raw.get("team") or raw.get("selection") or "",
            "odds": odds if odds > 1 else None,
            "book": raw.get("book") or raw.get("book_key") or "",
            "model_prob": round(base_p, 4),
            "effective_prob": round(eff_p, 4),
            "upset": up,
            "start": raw.get("start") or raw.get("start_time"),
        }
        out.append(leg)
    return out


def simulate_multi(legs: list, stake_lei: float = 2.0, n: int = SIM_N) -> dict:
    """Joint sim with upset haircut + honest OK / NU e OK (worth-risking odds)."""
    legs = normalize_legs(legs)
    n_legs = len(legs)
    stake = max(0.01, _f(stake_lei, 2.0))

    result: dict[str, Any] = {
        "ticket_type": "multi",
        "n_legs": n_legs,
        "legs": legs,
        "combined_odds": None,
        "joint_prob": None,
        "joint_prob_raw": None,
        "ev_pct": None,
        "hit_rate_sim": None,
        "expected_profit_lei": None,
        "potential_return_lei": None,
        "potential_profit_lei": None,
        "verdict": "NU e OK",
        "verdict_ok": False,
        "recomandat": False,
        "why_ro": "",
        "flags": [],
        "sim_n": n,
        "upset_summary": [],
    }

    if n_legs < MIN_LEGS or n_legs > MAX_LEGS:
        result["why_ro"] = f"Multi trebuie sa aiba exact 2 sau 3 selectii (acum: {n_legs})."
        result["flags"].append("bad_leg_count")
        return result

    odds_list, probs_raw, probs_eff = [], [], []
    for leg in legs:
        o = leg.get("odds")
        if not o or o <= 1:
            result["why_ro"] = "Cote lipsa sau invalide pe cel putin un leg."
            result["flags"].append("missing_odds")
            return result
        odds_list.append(float(o))
        probs_raw.append(float(leg.get("model_prob") or 0))
        probs_eff.append(float(leg.get("effective_prob") or 0))
        result["upset_summary"].append({
            "selection": leg.get("selection"),
            "level": (leg.get("upset") or {}).get("level"),
            "upset_rate": (leg.get("upset") or {}).get("upset_rate"),
            "note_ro": (leg.get("upset") or {}).get("note_ro"),
        })

    combined = 1.0
    for o in odds_list:
        combined *= o
    combined = round(combined, 3)
    result["combined_odds"] = combined
    result["potential_return_lei"] = round(combined * stake, 2)
    result["potential_profit_lei"] = round(combined * stake - stake, 2)

    if any(p <= 0 for p in probs_eff):
        result["why_ro"] = "Probabilitati lipsa — nu putem simula onest."
        result["flags"].append("missing_prob")
        return result

    joint_raw = 1.0
    for p in probs_raw:
        joint_raw *= p
    joint = 1.0
    for p in probs_eff:
        joint *= p
    result["joint_prob_raw"] = round(joint_raw, 4)
    result["joint_prob"] = round(joint, 4)
    ev = joint * combined - 1.0
    result["ev_pct"] = round(ev * 100, 2)
    result["expected_profit_lei"] = round(ev * stake, 2)

    rng = random.Random(42 + int(combined * 1000) + n_legs)
    hits = 0
    for _ in range(n):
        ok = True
        for p in probs_eff:
            if rng.random() > p:
                ok = False
                break
        if ok:
            hits += 1
    hit_rate = hits / float(n)
    result["hit_rate_sim"] = round(hit_rate * 100, 2)

    flags = []
    min_c = MIN_COMBINED_ODDS_2 if n_legs == 2 else MIN_COMBINED_ODDS_3
    max_c = MAX_COMBINED_ODDS_2 if n_legs == 2 else MAX_COMBINED_ODDS_3
    if combined > max_c:
        flags.append("combined_odds_fantasy")
    if combined < min_c:
        flags.append("combined_odds_juice")  # not worth the multi risk
    for o in odds_list:
        if o > MAX_LEG_ODDS:
            flags.append("leg_longshot")
        if o < MIN_LEG_ODDS:
            flags.append("leg_tiny_juice")
    for leg in legs:
        up = leg.get("upset") or {}
        if up.get("block_safe_multi"):
            flags.append("upset_prone_leg")
        elif up.get("yellow_flag"):
            flags.append("upset_yellow")

    if ev < MIN_EV_OK:
        flags.append("negative_ev")
    if joint < (0.26 if n_legs == 2 else 0.15):
        flags.append("low_joint_prob")

    result["flags"] = sorted(set(flags))

    hard_fail = any(
        f in result["flags"]
        for f in (
            "combined_odds_fantasy",
            "combined_odds_juice",
            "leg_longshot",
            "leg_tiny_juice",
            "negative_ev",
            "low_joint_prob",
            "upset_prone_leg",
            "missing_odds",
        )
    )
    soft_ok = (
        not hard_fail
        and min_c <= combined <= max_c
        and ev >= MIN_EV_OK
        and all(MIN_LEG_ODDS <= o <= MAX_LEG_ODDS for o in odds_list)
    )

    if soft_ok:
        result["verdict"] = "OK"
        result["verdict_ok"] = True
        # recomandat only if clearly +EV after upset haircut and no yellow upsests
        if ev >= 0.03 and "upset_yellow" not in result["flags"]:
            result["recomandat"] = True
        tip = "Recomandat (miza mica)." if result["recomandat"] else "Acceptabil dupa haircut surprize — nu e sigur."
        result["why_ro"] = (
            f"Multi {n_legs} @ {combined:.2f}: joint(eff) ~{joint*100:.1f}% "
            f"(raw {joint_raw*100:.1f}%), EV {ev*100:+.1f}%, hit-sim {hit_rate*100:.1f}%. {tip}"
        )
    else:
        result["verdict"] = "NU e OK"
        result["verdict_ok"] = False
        result["recomandat"] = False
        reasons = []
        mp = {
            "combined_odds_fantasy": f"cota combinata {combined:.2f} prea mare (plafon {max_c})",
            "combined_odds_juice": f"cota combinata {combined:.2f} prea mica — juice, nu merita riscul multi",
            "leg_longshot": f"leg longshot (> {MAX_LEG_ODDS})",
            "leg_tiny_juice": f"leg cu cotă < {MIN_LEG_ODDS} (juice fara EV)",
            "negative_ev": f"EV negativ dupa haircut surprize ({ev*100:.1f}%)",
            "low_joint_prob": f"prob. comuna efectiva mica ({joint*100:.1f}%)",
            "upset_prone_leg": "echipa surprise-prone pe un leg — nu e 'safe' pe multi",
        }
        for f in result["flags"]:
            if f in mp:
                reasons.append(mp[f])
        if not reasons:
            reasons.append("filtre / sim: risc prea mare")
        result["why_ro"] = "NU e OK: " + "; ".join(reasons) + "."

    return result


def enrich_ticket_multi(ticket: dict) -> dict:
    t = dict(ticket or {})
    legs = t.get("legs") or (t.get("source_tip") or {}).get("legs") or []
    if not legs and t.get("ticket_type") != "multi":
        t.setdefault("ticket_type", "single")
        # single upset badge from tip fields
        up = upset_profile(t.get("source_tip") or t)
        t["upset"] = up
        return t
    if len(legs) < 2:
        t.setdefault("ticket_type", "single")
        up = upset_profile(t.get("source_tip") or t)
        t["upset"] = up
        return t
    stake = t.get("stake_lei") if t.get("stake_lei") is not None else 2.0
    sim = simulate_multi(legs, stake_lei=_f(stake, 2.0))
    t["ticket_type"] = "multi"
    t["legs"] = sim["legs"]
    t["multi_sim"] = sim
    t["odds"] = sim.get("combined_odds") or t.get("odds")
    t["potential_return_lei"] = sim.get("potential_return_lei")
    t["potential_profit_lei"] = sim.get("potential_profit_lei")
    t["verdict"] = sim.get("verdict")
    t["verdict_ok"] = sim.get("verdict_ok")
    t["recomandat"] = bool(sim.get("recomandat"))
    t["multi_why"] = sim.get("why_ro")
    t["upset_summary"] = sim.get("upset_summary")
    if not t.get("selection"):
        t["selection"] = " + ".join(
            f"{lg.get('selection')}@{lg.get('odds')}" for lg in sim["legs"]
        )
    if not t.get("match"):
        t["match"] = " | ".join(lg.get("match") or "?" for lg in sim["legs"])
    t["market"] = t.get("market") or f"Multi {sim['n_legs']}"
    return t
