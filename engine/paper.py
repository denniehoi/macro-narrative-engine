"""Papieren handel: rendement per mandje sinds instap (en bij uitstap) en een papieren portefeuille.

- Instap- en uitstapkoers = laatste slotkoers bij de run die het mandje opent of sluit.
- Rendement in euro's: koers x wisselkoers. Het lokale rendement (zonder valuta-effect) staat ernaast.
- Gewichten zijn kopen-en-vasthouden vanaf instap; cash binnen een mandje (door caps) rendeert 0%.
- Vergelijking: de wereldindex (ACWI) in euro's over dezelfde periode.
"""
from __future__ import annotations

from typing import Optional


def eur_ratio(entry_price, entry_fx, price, fx) -> Optional[float]:
    if not entry_price or not entry_fx or not price or not fx:
        return None
    return (price * fx) / (entry_price * entry_fx)


def basket_perf(members: list[dict], prices: dict, fxs: dict, bench_entry_eur: Optional[float],
                bench_now_eur: Optional[float]) -> dict:
    """members: [{symbol, weight, entry_price, entry_fx}]; prices/fxs: actueel per symbool."""
    per, ret_eur, ret_local, missing = {}, 0.0, 0.0, []
    for m in members:
        s = m["symbol"]
        r = eur_ratio(m.get("entry_price"), m.get("entry_fx"), prices.get(s), fxs.get(s))
        if r is None:
            missing.append(s)
            continue
        local = prices[s] / m["entry_price"]
        per[s] = {"eur": r - 1.0, "local": local - 1.0}
        ret_eur += m["weight"] * (r - 1.0)
        ret_local += m["weight"] * (local - 1.0)
    bench = (bench_now_eur / bench_entry_eur - 1.0) if bench_entry_eur and bench_now_eur else None
    return {"return_eur": ret_eur, "return_local": ret_local, "bench_eur": bench,
            "excess_eur": (ret_eur - bench) if bench is not None else None,
            "per_symbol": per, "missing": missing, "bench_entry_eur": bench_entry_eur}


def mark_to_market(holdings: dict, cash: float, prices: dict, fxs: dict, last_values: dict) -> tuple[float, dict]:
    """holdings: {symbol: aantal}. Ontbrekende koers: houd de laatst bekende waarde vast."""
    values = {}
    for s, shares in holdings.items():
        p, f = prices.get(s), fxs.get(s)
        values[s] = shares * p * f if p and f else last_values.get(s, 0.0)
    return cash + sum(values.values()), values


def rebalance(nav: float, targets: list[dict], prices: dict, fxs: dict) -> tuple[dict, float, dict]:
    """Verdeel de papieren waarde volgens de doelgewichten. Zonder koers blijft dat deel cash."""
    holdings, values = {}, {}
    for t in targets:
        s, w = t["symbol"], t["weight"]
        p, f = prices.get(s), fxs.get(s)
        if not p or not f or w <= 0:
            continue
        values[s] = nav * w
        holdings[s] = values[s] / (p * f)
    return holdings, nav - sum(values.values()), values
