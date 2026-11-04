"""Synthetische wereld voor tests en `python -m engine simulate` (geen API-sleutels nodig).

Vijf thema's: twee structurele die de drempel halen, een shock met mutatie, een thema met te korte
horizon en ruis die discovery moet negeren.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

THEMES = {
    "ai": {"title": "AI-stroom en netcapaciteit", "marker": "data center power", "start": 0, "regime": "structural",
           "keywords": ["data center power", "grid capacity", "power purchase agreement"], "mmp": 88,
           "horizon": "gt_12m", "bench": [("XLU", 1), ("PAVE", 1)], "base": 220, "growth": 0.025,
           "prefix": "PWR"},
    "copper": {"title": "Kopertekort door elektrificatie", "marker": "copper shortage", "start": 1,
               "regime": "structural", "keywords": ["copper shortage", "copper mine supply", "copper smelter"],
               "mmp": 80, "horizon": "6_12m", "bench": [("HG=F", 1), ("COPX", 1)], "base": 140, "growth": 0.01,
               "prefix": "CU"},
    "hormuz": {"title": "Spanning Straat van Hormuz", "marker": "Strait of Hormuz", "start": 6, "regime": "shock",
               "keywords": ["Strait of Hormuz", "tanker attack", "Gulf shipping"], "mmp": 92, "horizon": "lt_3m",
               "bench": [("BZ=F", 1)], "base": 60, "growth": 0.0, "spike": 8, "prefix": "TNK", "high_impact": True},
    "ecb": {"title": "ECB-renteverlaging december", "marker": "ECB rate cut", "start": 2, "regime": "structural",
            "keywords": ["ECB rate cut", "Lagarde", "euro area inflation"], "mmp": 75, "horizon": "lt_3m",
            "bench": [("VGK", 1)], "base": 300, "growth": 0.0, "prefix": "EUB"},
    "gossip": {"title": "Celebrity wedding", "marker": "celebrity wedding", "start": 0, "regime": "shock",
               "keywords": ["celebrity wedding"], "mmp": 5, "horizon": "lt_3m", "bench": [], "base": 900,
               "growth": 0.0, "prefix": "GOS"},
}

TREND = {"XLU": 0.0025, "PAVE": 0.002, "HG=F": 0.003, "COPX": 0.003}


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def theme_for(keywords) -> str | None:
    text = " ".join(keywords or []).lower()
    for key, th in THEMES.items():
        if any(k.lower() in text for k in th["keywords"]):
            return key
    return None


class FakeWorld:
    def __init__(self, start: date):
        self.start = start

    def idx(self, day: date) -> int:
        return (day - self.start).days

    def volume(self, key: str, day: date) -> int:
        th = THEMES[key]
        i = self.idx(day)
        if i < th["start"] - 25:
            return 0
        rng = np.random.default_rng(_seed(key, day))
        v = th["base"] * math.exp(th["growth"] * max(0, i - th["start"]))
        if "spike" in th and i >= th["start"]:
            age = i - th["start"]
            v *= 1 + th["spike"] * math.exp(-age / 6)
        return max(0, int(v * rng.uniform(0.85, 1.15)))


class FakeNews:
    def __init__(self, world: FakeWorld):
        self.w = world
        self.spent = 0.0

    def topic_articles(self, keywords, day):
        self.spent += 1
        key = theme_for(keywords)
        if key is None:
            return {"total": 3, "articles": [{"title": "Unrelated story", "body": "", "source": "x"}]}
        th = THEMES[key]
        total = self.w.volume(key, day)
        rng = np.random.default_rng(_seed("arts", key, day))
        arts = []
        for j in range(min(20, total)):
            on = rng.random() < 0.82
            title = f"{th['marker'].title()} update {j}" if on else f"Market wrap {j}"
            arts.append({"title": title, "body": title, "source": "Fake Wire",
                         "sentiment": float(rng.normal(-0.05, 0.2))})
        return {"total": total, "articles": arts}

    def topic_counts(self, keywords, end, days):
        self.spent += 5
        key = theme_for(keywords)
        out = {}
        for k in range(days):
            d = end - timedelta(days=k)
            out[d.isoformat()] = self.w.volume(key, d) if key else 3
        return out

    def discovery_events(self, day):
        self.spent += 5
        evs = []
        for key, th in THEMES.items():
            if self.w.idx(day) >= th["start"] - 1:
                evs.append({"uri": f"evt-{key}-{day}", "title": f"{th['title']} ({th['marker']})",
                            "summary": th["marker"], "articles": self.w.volume(key, day), "concepts": th["keywords"]})
        return evs


class FakeLLM:
    def __init__(self):
        self.usage = {"input": 0, "output": 0, "web_searches": 0}

    def structured(self, model_key, system, payload, tool_name, schema, max_tokens=None):
        self.usage["input"] += 1000
        self.usage["output"] += 200
        return getattr(self, "_" + tool_name)(payload)

    def research(self, model_key, system, payload, tool_name, schema, max_rounds=6):
        self.usage["web_searches"] += 5
        key = theme_for(payload.get("keywords"))
        prefix = THEMES[key]["prefix"] if key else "GEN"
        cands = []
        roles = ["pure_play"] * 4 + ["pick_and_shovel"] * 4 + ["second_order"] * 3
        exposures = [80, 65, 55, None, 40, 35, 30, 25, 15, 10, 8]
        for i, (role, exp) in enumerate(zip(roles, exposures)):
            cands.append({"symbol": f"{prefix}{i}", "name": f"{prefix} Corp {i}", "role": role,
                          "exposure_pct": exp, "pre_revenue": exp is None,
                          "source_url": f"https://example.com/{prefix}{i}", "evidence": "synthetisch"})
        cands.append({"symbol": "NEWCO", "name": "Nieuwe notering", "role": "pure_play", "exposure_pct": 90,
                      "pre_revenue": False, "source_url": "https://example.com/newco", "evidence": "IPO"})
        cands.append({"symbol": "ILLIQ", "name": "Illiquide", "role": "pure_play", "exposure_pct": 90,
                      "pre_revenue": False, "source_url": "https://example.com/illiq", "evidence": "klein"})
        return {"candidates": cands}

    # --- tool-antwoorden -------------------------------------------------------------
    def _submit_discovery(self, p):
        existing = " ".join(t["title"] + " " + t["driver"] for t in p["bestaande_topics"]).lower()
        out = []
        for e in p["events"]:
            key = theme_for(e["concepts"])
            th = THEMES[key]
            dup = next((t["slug"] for t in p["bestaande_topics"]
                        if th["marker"].lower() in " ".join(t.get("keywords", [])).lower()
                        or t["title"] == th["title"]), None)
            out.append({"event_uris": [e["uri"]], "title": th["title"],
                        "main_macro_driver": f"{th['title']}: mechanisme via {th['marker']}",
                        "keywords": th["keywords"], "mmp": th["mmp"], "transmission": "synthetisch",
                        "regime": th["regime"], "high_impact": th.get("high_impact", False),
                        "duplicate_of": dup})
        return {"candidates": out}

    def _submit_alignment(self, p):
        arts = p["artikelen"]
        on = sum(1 for a in arts if "update" in (a["titel"] or "").lower())
        return {"alignment": on / len(arts) if arts else 0, "on_thesis_count": on,
                "backward_looking_share": 0.05, "emerging_tags": ["synthetisch deelthema"], "note": "fake"}

    def _submit_reasoning(self, p):
        t = p["topic"]
        key = theme_for(t["keywords"])
        th = THEMES.get(key or "", {})
        mutate = p.get("mutation_check") and key == "hormuz"
        return {
            "horizon_class": "6_12m" if (key == "hormuz" and "Tanker" in t["title"]) else th.get("horizon", "3_6m"),
            "horizon_reason": "synthetisch",
            "benchmarks": [{"symbol": s, "direction": d, "why": "synthetisch"} for s, d in th.get("bench", [])],
            "polymarket_slugs": ["hormuz-closure-2026"] if key == "hormuz" else [],
            "resolved": False, "resolved_reason": "",
            "mutation": {"mutate": bool(mutate), "new_title": "Tankervracht en olie-risicopremie",
                         "new_driver": "Blijvend hogere verzekerings- en vrachtkosten voor tankers via de Golf",
                         "new_keywords": ["Strait of Hormuz", "tanker rates", "war risk insurance"],
                         "reason": "verzekeringspremies blijven hoog"},
            "driver_update": {"update": False, "reason": ""},
            "overlaps_with": [],
        }

    def _submit_basket(self, p):
        members = [{"symbol": c["symbol"], "subniche": c["role"], "exposure_pct": c["exposure_pct"],
                    "pre_revenue": c["pre_revenue"], "rationale": f"{c['name']} past bij het thema"}
                   for c in p["kandidaten"][:7]]
        return {"members": members, "excluded": []}


class FakePoly:
    MARKET = {"slug": "hormuz-closure-2026", "question": "Will Iran close the Strait of Hormuz by March 31?",
              "event": "Hormuz", "prob_yes": 0.18, "one_day_change": 0.04, "liquidity": 250000.0,
              "volume": 1_200_000.0, "end_date": "2027-03-31", "closed": False, "active": True}

    def search(self, query):
        return [dict(self.MARKET)] if "hormuz" in query.lower() else []

    def market(self, slug):
        return dict(self.MARKET) if slug == self.MARKET["slug"] else None


class FakeMarket:
    def __init__(self, world: FakeWorld):
        self.w = world
        self.today = world.start
        self.stale_symbols: set[str] = set()
        self._cache: dict[str, tuple[pd.Series, pd.Series]] = {}
        self.index = pd.bdate_range(world.start - timedelta(days=700), world.start + timedelta(days=200))

    def _series(self, sym):
        if sym in self._cache:
            return self._cache[sym]
        rng = np.random.default_rng(_seed("px", sym))
        n = len(self.index)
        vol = 0.012 if sym.endswith("=F") or sym in TREND or sym in ("ACWI", "SPY") else rng.uniform(0.015, 0.04)
        rets = rng.normal(0.0002, vol, n)
        start_i = self.index.searchsorted(pd.Timestamp(self.w.start - timedelta(days=60)))
        if sym in TREND:
            rets[start_i:] += TREND[sym]
        if sym == "BZ=F":
            spike_i = self.index.searchsorted(pd.Timestamp(self.w.start + timedelta(days=5)))
            rets[spike_i:spike_i + 4] += 0.03
        prefix_trend = {"PWR": 0.0015, "CU": 0.001, "TNK": 0.002}
        for pre, tr in prefix_trend.items():
            if sym.startswith(pre):
                rets[start_i:] += tr
        close = pd.Series(rng.uniform(30, 200) * np.exp(np.cumsum(rets)), index=self.index)
        volume = pd.Series(rng.uniform(1.5e6, 4e6, n), index=self.index)
        if sym == "ILLIQ":
            volume[:] = 2000.0
        if sym == "NEWCO":  # recente beursgang: te weinig historie
            close[close.index < pd.Timestamp(self.w.start - timedelta(days=30))] = np.nan
        self._cache[sym] = (close, volume)
        return self._cache[sym]

    def load(self, symbols, today):
        self.today = today

    def _cut(self, s):
        return s[s.index < pd.Timestamp(self.today)]

    def close(self, sym):
        return self._cut(self._series(sym)[0])

    def volume(self, sym):
        return self._cut(self._series(sym)[1])

    def closes(self, symbols):
        return pd.DataFrame({s: self.close(s) for s in symbols})

    def last_price(self, sym):
        s = self.close(sym).dropna()
        return float(s.iloc[-1]) if len(s) else None

    def currency(self, sym):
        return "EUR" if sym.endswith(".AS") else "USD"

    def fx_to_eur(self, cur):
        return 1.0 if cur == "EUR" else 0.92
