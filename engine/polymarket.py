"""Polymarket Gamma API: context voor de Reasoning LLM. Nooit een veto; fouten tellen neutraal."""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)


def _loads(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalize_market(m: dict, event_title: Optional[str] = None) -> dict:
    outcomes = _loads(m.get("outcomes")) or []
    prices = [_num(p) for p in (_loads(m.get("outcomePrices")) or [])]
    prob = None
    if prices:
        idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
        prob = prices[idx] if idx < len(prices) else prices[0]
    return {
        "slug": m.get("slug"),
        "question": m.get("question"),
        "event": event_title,
        "prob_yes": prob,
        "one_day_change": _num(m.get("oneDayPriceChange")) if m.get("oneDayPriceChange") is not None else None,
        "liquidity": _num(m.get("liquidityNum") or m.get("liquidity")),
        "volume": _num(m.get("volumeNum") or m.get("volume")),
        "end_date": (m.get("endDate") or "")[:10] or None,
        "closed": bool(m.get("closed")),
        "active": bool(m.get("active", True)),
    }


class PolymarketClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg["polymarket"]
        self.http = httpx.Client(base_url=self.cfg["base_url"], timeout=30)

    def search(self, query: str) -> list[dict]:
        try:
            r = self.http.get("/public-search", params={"q": query})
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001 - Polymarket is optioneel
            log.warning("Polymarket search faalde (%s): %s", query, e)
            return []
        markets = []
        for ev in data.get("events", []) or []:
            for m in ev.get("markets", []) or []:
                markets.append(normalize_market(m, ev.get("title")))
        markets = [m for m in markets if m["slug"] and not m["closed"]
                   and m["liquidity"] >= self.cfg["min_liquidity_usd"]]
        markets.sort(key=lambda m: m["liquidity"], reverse=True)
        return markets[: self.cfg["max_markets_per_topic"]]

    def market(self, slug: str) -> Optional[dict]:
        try:
            r = self.http.get("/markets", params={"slug": slug})
            r.raise_for_status()
            rows = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Polymarket market faalde (%s): %s", slug, e)
            return None
        rows = rows if isinstance(rows, list) else rows.get("data", [])
        return normalize_market(rows[0]) if rows else None
