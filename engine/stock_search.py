"""Stock Search-fase: aparte LLM-stap bij promotie, mutatie, nieuwe versie en maandelijkse review.

1. LLM met web search stelt per rol kandidaten voor, met bron voor de omzetblootstelling.
2. Python filtert: bestaat, >= 60 dagen historie, liquide genoeg, niet al in een ander mandje.
3. LLM kiest 4-8 namen; de tier volgt hard uit exposure_pct; Python rekent de gewichten.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import metrics, prompts, weighting

log = logging.getLogger(__name__)


def _usd_addv(market, sym: str) -> Optional[float]:
    c, v = market.close(sym), market.volume(sym)
    if c is None or v is None:
        return None
    a = metrics.addv(c, v, 20)
    if a is None:
        return None
    cur = market.currency(sym)
    if cur == "USD":
        return a
    eur = market.fx_to_eur(cur)
    market.fx_to_eur("USD")  # laadt EURUSD=X
    usd_per_eur = market.last_price("EURUSD=X") if eur else None
    if not eur or not usd_per_eur:
        return None
    return a * eur * usd_per_eur


def filter_candidates(cands: list[dict], market, taken: set[str], cfg: dict, today) -> tuple[list[dict], list[dict]]:
    ss = cfg["stock_search"]
    seen, keep, dropped = set(), [], []
    symbols = [c["symbol"].strip().upper() for c in cands]
    market.load(symbols, today)
    for c, sym in zip(cands, symbols):
        if sym in seen:
            continue
        seen.add(sym)
        reason = None
        close = market.close(sym)
        if sym in taken:
            reason = "zit al in een ander mandje"
        elif close is None or metrics.history_days(close) < ss["min_history_days"]:
            reason = f"minder dan {ss['min_history_days']} dagen koershistorie of onbekend"
        else:
            liq = _usd_addv(market, sym)
            if liq is None or liq < ss["min_addv_usd"]:
                reason = "te weinig dagomzet"
        if reason:
            dropped.append({"symbol": sym, "reason": reason})
            continue
        sigma = metrics.realized_vol(close, cfg["score"]["vol_window"])
        keep.append({**c, "symbol": sym, "sigma": sigma, "addv_usd": _usd_addv(market, sym),
                     "currency": market.currency(sym)})
    return keep, dropped


def run_stock_search(topic: dict, llm, market, taken: set[str], cfg: dict, today) -> dict:
    """Geeft {"members": [...], "cash": x, "valid": bool, "notes": {...}}."""
    ss = cfg["stock_search"]
    step1 = llm.research(
        "stock_search", prompts.STOCK_CANDIDATES_SYSTEM.format(n=ss["candidates_per_role"]),
        {"topic": topic["title"], "these": topic["main_macro_driver"], "keywords": topic.get("keywords"),
         "horizon": topic.get("horizon_class")},
        "submit_candidates", prompts.STOCK_CANDIDATES_SCHEMA)
    cands = step1.get("candidates", [])
    keep, dropped = filter_candidates(cands, market, taken, cfg, today)
    notes = {"candidates": len(cands), "filtered_out": dropped}
    if len(keep) < ss["min_tickers"]:
        return {"members": [], "cash": 1.0, "valid": False,
                "notes": {**notes, "reason": f"maar {len(keep)} geldige kandidaten"}}
    step3 = llm.structured(
        "stock_search",
        prompts.STOCK_SELECT_SYSTEM.format(min_n=ss["min_tickers"], max_n=ss["max_tickers"]),
        {"topic": topic["title"], "these": topic["main_macro_driver"],
         "kandidaten": [{k: c.get(k) for k in ("symbol", "name", "role", "exposure_pct", "pre_revenue",
                                               "evidence", "source_url", "sigma", "addv_usd")} for c in keep]},
        "submit_basket", prompts.STOCK_SELECT_SCHEMA)
    by_sym = {c["symbol"]: c for c in keep}
    chosen = [m for m in step3.get("members", []) if m["symbol"] in by_sym][: ss["max_tickers"]]
    notes["excluded_by_llm"] = step3.get("excluded", [])
    if len(chosen) < ss["min_tickers"]:
        return {"members": [], "cash": 1.0, "valid": False,
                "notes": {**notes, "reason": f"LLM koos {len(chosen)} namen"}}
    members = build_members(chosen, by_sym, cfg)
    corr = metrics.corr_matrix(market.closes([m["symbol"] for m in members]), cfg["score"]["vol_window"])
    return {**apply_weights(members, corr, cfg), "notes": notes}


def build_members(chosen: list[dict], by_sym: dict, cfg: dict) -> list[dict]:
    members = []
    for m in chosen:
        c = by_sym[m["symbol"]]
        pre = bool(m.get("pre_revenue", c.get("pre_revenue")))
        exp = m.get("exposure_pct", c.get("exposure_pct"))
        exp = None if exp is None else max(0.0, min(100.0, float(exp)))
        members.append({
            "symbol": m["symbol"], "name": c.get("name"), "role": str(c.get("role") or "").lower() or None,
            "tier": weighting.tier_from_exposure(exp, pre, cfg), "exposure_pct": exp, "pre_revenue": pre,
            "rationale": m.get("rationale"), "source_url": c.get("source_url"), "sigma": c.get("sigma"),
            "addv_usd": c.get("addv_usd"), "currency": c.get("currency"),
        })
    return members


def apply_weights(members: list[dict], corr, cfg: dict) -> dict:
    weights, cash, clusters = weighting.basket_weights(members, corr, cfg)
    for m in members:
        m["weight"] = round(weights.get(m["symbol"], 0.0), 6)
        m["cluster"] = clusters.get(m["symbol"])
    return {"members": members, "cash": cash, "valid": len(members) >= cfg["stock_search"]["min_tickers"]}
