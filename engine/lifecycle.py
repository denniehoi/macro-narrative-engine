"""Levenscyclus van topics: streaks, uitstappen, instappen met verdringing, topicgewichten."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Optional


def update_streaks(topic: dict, i: Optional[float], gates_pass: bool, cfg: dict) -> None:
    a, od = cfg["active"], cfg["on_deck"]
    qualifies = gates_pass and i is not None and i >= a["enter_threshold"]
    topic["streak_in"] = topic.get("streak_in", 0) + 1 if qualifies else 0
    topic["gate_fail_streak"] = topic.get("gate_fail_streak", 0) + 1 if not gates_pass else 0
    low = i is None or i < od["purge_below_i"]
    topic["low_i_streak"] = topic.get("low_i_streak", 0) + 1 if low else 0


def is_protected(topic: dict, today: date) -> bool:
    pu = topic.get("protected_until")
    if not pu:
        return False
    return date.fromisoformat(str(pu)) >= today


def should_exit(topic: dict, i: Optional[float], today: date, cfg: dict) -> Optional[str]:
    """Uitstapregel voor een Active topic. Geeft een reden, of None om te blijven."""
    fail_days = cfg["gates"]["fail_days_to_exit"]
    if topic.get("gate_fail_streak", 0) >= fail_days:
        return f"harde gate faalt {fail_days} dagen"
    if is_protected(topic, today):
        return None
    if i is None or i < cfg["active"]["exit_threshold"]:
        return f"I onder {cfg['active']['exit_threshold']}"
    return None


def select_active(incumbents: list[dict], challengers: list[dict], cfg: dict,
                  try_enter: Callable[[dict], bool], today: date) -> tuple[list[dict], list[dict]]:
    """Vul maximaal `max_topics` plekken.

    incumbents: Active topics die blijven (met '_i').
    challengers: kandidaten op volgorde (goedgekeurde shocks eerst, dan hoogste I).
    try_enter: voert Stock Search uit; False = gate 3 faalt, topic blijft On Deck.
    Geeft (nieuwe Active-lijst, verdrongen topics).
    """
    slots = cfg["active"]["max_topics"]
    margin = cfg["active"]["displacement_margin"]
    current = list(incumbents)
    displaced: list[dict] = []
    def protect(t: dict) -> None:
        # Een goedgekeurde shock krijgt na binnenkomst een beschermperiode (geen I-exit, niet verdringbaar).
        if t.get("_approved_shock") and not t.get("protected_until"):
            days = cfg["shock_radar"]["protection_days"]
            t["protected_until"] = (today + timedelta(days=days)).isoformat()

    for ch in challengers:
        approved = bool(ch.get("_approved_shock"))
        if len(current) < slots:
            if try_enter(ch):
                protect(ch)
                current.append(ch)
            continue
        removable = [t for t in current if not is_protected(t, today)]
        if not removable:
            continue
        lowest = min(removable, key=lambda t: t.get("_i") or 0.0)
        beats = (ch.get("_i") or 0.0) > (lowest.get("_i") or 0.0) * (1.0 + margin)
        if (approved or beats) and try_enter(ch):
            protect(ch)
            current.remove(lowest)
            displaced.append(lowest)
            current.append(ch)
    return current, displaced


def capped_proportional(values: dict, caps: dict | float, total: float = 1.0) -> dict:
    """Water-filling: gewichten evenredig met `values`, elk <= cap, som <= total.

    Overschot van gecapte posten gaat naar de rest; als alles gecapt is, blijft het restant over.
    """
    if isinstance(caps, (int, float)):
        caps = {k: float(caps) for k in values}
    weights: dict = {}
    remaining = {k for k, v in values.items() if v and v > 0}
    budget = total
    while remaining and budget > 1e-12:
        s = sum(values[k] for k in remaining)
        trial = {k: budget * values[k] / s for k in remaining}
        over = [k for k in remaining if trial[k] > caps[k] + 1e-12]
        if not over:
            weights.update(trial)
            break
        for k in over:
            weights[k] = caps[k]
            budget -= caps[k]
            remaining.discard(k)
    for k in values:
        weights.setdefault(k, 0.0)
    return weights


def topic_weights(active: list[dict], cfg: dict) -> tuple[dict, float]:
    """Gewicht per topic evenredig met I, max 25% per topic; restant is cash."""
    vals = {t["id"]: max(0.0, t.get("_i") or 0.0) for t in active}
    w = capped_proportional(vals, cfg["active"]["topic_cap"])
    cash = max(0.0, 1.0 - sum(w.values()))
    return w, cash
