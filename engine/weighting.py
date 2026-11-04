"""Weging binnen een mandje en over de hele portefeuille.

1. Vaste budgetten per tier (40/40/20), lege tier naar rato verdeeld.
2. Binnen elke tier inverse volatiliteit met sigma-ondergrens.
3. Paren met correlatie > 0,85 delen één gewicht (cluster).
4. Caps: 30% per ticker in een mandje (pre-revenue lager), 10% per ticker in de portefeuille,
   en een liquiditeitslimiet (positie <= 1% van de dagomzet).
5. Rebalance maandelijks, bij gewijzigde samenstelling, of bij drift > 5 procentpunt.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from .lifecycle import capped_proportional

TIERS = ("pure_play", "pick_and_shovel", "second_order")


def tier_from_exposure(exposure_pct: Optional[float], pre_revenue: bool, cfg: dict) -> str:
    """Tier volgt hard uit het omzetaandeel. Pre-revenue telt als pure play met eigen (lagere) cap."""
    th = cfg["weighting"]["tier_exposure_pct"]
    if pre_revenue:
        return "pure_play"
    if exposure_pct is None:
        return "second_order"
    if exposure_pct >= th["pure_play"]:
        return "pure_play"
    if exposure_pct >= th["pick_and_shovel"]:
        return "pick_and_shovel"
    return "second_order"


def corr_clusters(names: list[str], corr: Optional[pd.DataFrame], threshold: float) -> list[list[str]]:
    """Union-find op paren met correlatie boven de drempel."""
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if corr is not None:
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if a in corr.index and b in corr.columns:
                    c = corr.loc[a, b]
                    if pd.notna(c) and c > threshold:
                        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return [sorted(g) for g in groups.values()]


def basket_weights(members: list[dict], corr: Optional[pd.DataFrame], cfg: dict) -> tuple[dict, float, dict]:
    """members: [{symbol, tier, sigma, pre_revenue}]. Geeft (gewichten, cash-aandeel, cluster-id per symbool)."""
    w = cfg["weighting"]
    floor = w["sigma_floor"]
    present = {t for t in TIERS if any(m["tier"] == t for m in members)}
    if not present:
        return {}, 1.0, {}
    total_budget = sum(w["tier_budgets"][t] for t in present)
    raw: dict[str, float] = {}
    cluster_of: dict[str, int] = {}
    cid = 0
    for tier in TIERS:
        if tier not in present:
            continue
        budget = w["tier_budgets"][tier] / total_budget
        tier_members = [m for m in members if m["tier"] == tier]
        sig = {m["symbol"]: max(floor, m.get("sigma") or floor) for m in tier_members}
        clusters = corr_clusters(sorted(sig), corr, w["corr_threshold"])
        scores = [1.0 / (sum(sig[s] for s in c) / len(c)) for c in clusters]
        ssum = sum(scores)
        for c, sc in zip(clusters, scores):
            cw = budget * sc / ssum
            inv = {s: 1.0 / sig[s] for s in c}
            isum = sum(inv.values())
            for s in c:
                raw[s] = cw * inv[s] / isum
                cluster_of[s] = cid
            cid += 1
    caps = {m["symbol"]: (w["pre_revenue_max_weight"] if m.get("pre_revenue") else w["max_weight_in_basket"])
            for m in members}
    final = capped_proportional(raw, caps, total=1.0)
    cash = max(0.0, 1.0 - sum(final.values()))
    return final, cash, cluster_of


def portfolio_targets(topic_w: dict, baskets: dict, addv_eur: dict, cfg: dict) -> tuple[dict, float]:
    """baskets: {topic_id: {symbol: gewicht_in_mandje}}. Geeft ({symbol: {weight, topics}}, cash)."""
    w = cfg["weighting"]
    agg: dict[str, dict] = {}
    for tid, tw in topic_w.items():
        for sym, bw in baskets.get(tid, {}).items():
            e = agg.setdefault(sym, {"weight": 0.0, "topics": []})
            e["weight"] += tw * bw
            e["topics"].append(tid)
    for sym, e in agg.items():
        cap = w["max_weight_portfolio"]
        liq = addv_eur.get(sym)
        if liq:
            cap = min(cap, w["liquidity_max_share_of_addv"] * liq / w["portfolio_value_eur"])
        if e["weight"] > cap:
            e["capped_from"] = e["weight"]
            e["weight"] = cap
    cash = max(0.0, 1.0 - sum(e["weight"] for e in agg.values()))
    return agg, cash


def implied_drift(targets: dict, prices_then: dict, prices_now: dict) -> float:
    """Hoeveel de werkelijke gewichten door koersbewegingen van de doelen zijn afgedreven."""
    grown = {}
    for s, wt in targets.items():
        p0, p1 = prices_then.get(s), prices_now.get(s)
        grown[s] = wt * (p1 / p0) if p0 and p1 else wt
    cash = max(0.0, 1.0 - sum(targets.values()))
    total = sum(grown.values()) + cash
    if total <= 0:
        return 0.0
    return max((abs(grown[s] / total - targets[s]) for s in targets), default=0.0)


def needs_rebalance(last: Optional[dict], today: date, composition_changed: bool, drift: float,
                    cfg: dict) -> tuple[bool, Optional[str]]:
    if last is None or not last.get("last_rebalance_day"):
        return True, "eerste allocatie"
    if composition_changed:
        return True, "samenstelling gewijzigd"
    last_day = date.fromisoformat(str(last["last_rebalance_day"]))
    if cfg["rebalance"]["monthly"] and (today.year, today.month) != (last_day.year, last_day.month):
        return True, "maandelijkse rebalance"
    if drift > cfg["rebalance"]["drift_threshold"]:
        return True, f"drift {drift * 100:.1f} pp"
    return False, None
