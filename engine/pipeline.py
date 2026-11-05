"""Dagelijkse run: van nieuws tot doelgewichten.

Volgorde
  0. Goedkeuringen verwerken (verlopen, goedgekeurde shocks/merges)
  1. Bestaande topics meten (volume + alignment), Active eerst i.v.m. token-budget
  2. Discovery van nieuwe topics (+ eenmalige basislijn)
  3. Koersdata laden (benchmarks + mandjes)
  4. Narratiefscore R en volume-z bijwerken
  5. Reasoning (horizon, benchmarks, resolutie, mutatie, overlap) voor wie het nodig heeft
  6. N, M, D, P, I en de harde gates
  7. Shock-radar: goedkeuringsverzoeken; overlap: merges
  8. Levenscyclus: uitstappen, instappen (met Stock Search), opruimen, mandjes verversen
  9. Allocatie: topicgewichten (pie), portefeuille-doelen, rebalance-besluit
 10. Opslaan
"""
from __future__ import annotations

import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from . import lifecycle, metrics, prompts, scoring, stock_search
from .newsapi import TokenBudgetExceeded
from .repo import Repo

log = logging.getLogger(__name__)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "topic"


def _d(v) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


@dataclass
class Context:
    cfg: dict
    bench: dict
    repo: Repo
    news: Any
    poly: Any
    market: Any
    llm: Any
    dry_run: bool = False
    now: Optional[datetime] = None


@dataclass
class DailyRun:
    ctx: Context
    day: date
    errors: list = field(default_factory=list)
    events: list = field(default_factory=list)

    # --- hulp ---------------------------------------------------------------------
    @property
    def cfg(self):
        return self.ctx.cfg

    @property
    def repo(self) -> Repo:
        return self.ctx.repo

    @property
    def now(self) -> datetime:
        return self.ctx.now or datetime.combine(self.day, time(6, 0), tzinfo=timezone.utc)

    def event(self, kind: str, topic: Optional[dict] = None, **extra):
        e = {"kind": kind, **({"topic": topic["title"], "slug": topic["slug"]} if topic else {}), **extra}
        self.events.append(e)
        log.info("%s %s", kind, e)

    def error(self, where: str, exc: Exception):
        self.errors.append({"where": where, "error": f"{type(exc).__name__}: {exc}"})
        log.warning("Fout in %s: %s", where, exc)

    def tracked(self, *statuses):
        return [t for t in self.topics if t["status"] in statuses]

    # --- hoofdlus -----------------------------------------------------------------
    def run(self) -> dict:
        rec = self.repo.start_run(self.day, self.ctx.dry_run)
        status = "ok"
        try:
            self.topics = self.repo.tracked_topics()
            for t in self.topics:
                t["meta"] = t.get("meta") or {}
            self.obs: dict[str, dict] = {}
            self.approved_shocks: dict[str, dict] = {}
            self.stock_searches = 0
            self.handle_approvals()
            self.observe(self.priority_order(self.topics))
            self.discover()
            self.load_history()
            self.load_market()
            self.update_narrative()
            self.score_all()
            self.reason()
            self.score_all()
            self.shock_radar()
            self.merges()
            self.lifecycle()
            self.allocate()
            self.paper_trading()
            self.persist()
            if self.errors:
                status = "partial"
        except Exception as e:  # noqa: BLE001
            status = "failed"
            self.errors.append({"where": "run", "error": f"{type(e).__name__}: {e}",
                                "trace": traceback.format_exc()[-2000:]})
            log.exception("Run mislukt")
        finally:
            llm_usage = getattr(self.ctx.llm, "usage", {}) or {}
            summary = {
                "events": self.events,
                "active": [t["title"] for t in getattr(self, "topics", []) if t["status"] == "active"],
                "tracked": len(getattr(self, "topics", [])),
            }
            self.repo.finish_run(rec["id"], {
                "status": status, "summary": summary, "errors": self.errors,
                "newsapi_tokens": getattr(self.ctx.news, "spent", 0),
                "llm_input_tokens": llm_usage.get("input", 0), "llm_output_tokens": llm_usage.get("output", 0),
                "web_searches": llm_usage.get("web_searches", 0),
            })
        return {"status": status, "errors": self.errors, "events": self.events}

    # --- 0. goedkeuringen -----------------------------------------------------------
    def handle_approvals(self):
        by_id = {t["id"]: t for t in self.topics}
        for a in self.repo.approvals(["pending", "approved"]):
            if a["status"] == "pending":
                if datetime.fromisoformat(str(a["expires_at"]).replace("Z", "+00:00")) < self.now:
                    self.repo.set_approval(a["id"], {"status": "expired"})
                    self.event("goedkeuring_verlopen", by_id.get(a["topic_id"]))
                continue
            topic = by_id.get(a["topic_id"])
            if topic is None:
                self.repo.set_approval(a["id"], {"status": "applied", "note": "topic niet meer gevolgd"})
                continue
            if a["kind"] == "shock":
                topic["_approved_shock"] = True
                topic["_approval_id"] = a["id"]
                self.approved_shocks[topic["id"]] = a
            elif a["kind"] == "merge":
                other = by_id.get(a.get("other_topic_id"))
                if other:
                    self.merge_topics(topic, other, "goedgekeurde merge")
                self.repo.set_approval(a["id"], {"status": "applied"})

    # --- 1. meten -------------------------------------------------------------------
    @staticmethod
    def priority_order(topics):
        rank = {"active": 0, "radar": 1, "on_deck": 2}
        return sorted(topics, key=lambda t: (not t.get("_approved_shock"), rank.get(t["status"], 9),
                                             -(t.get("r_score") or 0)))

    def observe(self, topics):
        news_day = self.day - timedelta(days=1)
        for t in topics:
            if t["id"] in self.obs or t["status"] not in ("radar", "on_deck", "active"):
                continue
            o = {"articles": None, "alignment": None, "sentiment": None, "backward": None, "tags": [],
                 "headlines": []}
            self.obs[t["id"]] = o
            if getattr(self, "budget_exhausted", False):
                continue
            try:
                res = self.ctx.news.topic_articles(t["keywords"], news_day)
            except TokenBudgetExceeded as e:
                self.budget_exhausted = True
                self.error("nieuws", e)
                continue
            except Exception as e:  # noqa: BLE001
                self.error(f"nieuws {t['slug']}", e)
                continue
            o["articles"] = res["total"]
            arts = res["articles"]
            o["headlines"] = [a["title"] for a in arts[:12] if a.get("title")]
            o["sentiment"] = _mean([a.get("sentiment") for a in arts])
            if not arts:
                continue
            try:
                al = self.ctx.llm.structured(
                    "lite", prompts.ALIGNMENT_SYSTEM,
                    {"these": t["main_macro_driver"], "titel": t["title"],
                     "artikelen": [{"titel": a["title"], "tekst": a["body"], "bron": a.get("source")} for a in arts]},
                    "submit_alignment", prompts.ALIGNMENT_SCHEMA, max_tokens=800)
                o["alignment"] = scoring.clip01(float(al["alignment"]))
                o["backward"] = scoring.clip01(float(al.get("backward_looking_share", 0)))
                o["tags"] = al.get("emerging_tags", [])[:5]
            except Exception as e:  # noqa: BLE001
                self.error(f"alignment {t['slug']}", e)

    # --- 2. discovery -----------------------------------------------------------------
    def discover(self):
        news_day = self.day - timedelta(days=1)
        if getattr(self, "budget_exhausted", False):
            return
        try:
            events = self.ctx.news.discovery_events(news_day)
        except Exception as e:  # noqa: BLE001
            self.error("discovery", e)
            return
        if not events:
            return
        existing = [{"slug": t["slug"], "title": t["title"], "driver": t["main_macro_driver"],
                     "keywords": t.get("keywords") or []} for t in self.topics
                    if t["status"] in ("radar", "on_deck", "active")]
        try:
            out = self.ctx.llm.structured(
                "lite", prompts.DISCOVERY_SYSTEM, {"events": events, "bestaande_topics": existing},
                "submit_discovery", prompts.DISCOVERY_SCHEMA, max_tokens=8000)
        except Exception as e:  # noqa: BLE001
            self.error("discovery-llm", e)
            return
        cfg = self.cfg["discovery"]
        slugs = self.repo.all_slugs()
        for c in out.get("candidates", []):
            c["regime"] = "shock" if str(c.get("regime", "")).lower() == "shock" else "structural"
            c["mmp"] = max(0, min(100, int(c.get("mmp") or 0)))
        cands = [c for c in out.get("candidates", []) if not c.get("duplicate_of") and c["mmp"] >= cfg["mmp_threshold"]]
        cands.sort(key=lambda c: c["mmp"], reverse=True)
        for c in cands[: cfg["max_new_topics_per_day"]]:
            slug = slugify(c["title"])
            n = 2
            while slug in slugs:
                slug = f"{slugify(c['title'])}-{n}"
                n += 1
            slugs.add(slug)
            t = self.repo.create_topic({
                "slug": slug, "title": c["title"], "main_macro_driver": c["main_macro_driver"],
                "keywords": c["keywords"][:6], "status": "radar" if c["regime"] == "shock" else "on_deck",
                "regime": c["regime"], "mmp": c["mmp"], "high_impact": bool(c.get("high_impact")),
                "source_event_uris": c.get("event_uris", [])[:10], "first_seen": self.day,
                "meta": {"transmission": c.get("transmission")}, "version": 1, "benchmarks": [],
                "polymarket_slugs": [], "streak_in": 0, "gate_fail_streak": 0, "low_i_streak": 0,
            })
            t["meta"] = t.get("meta") or {}
            t["_new"] = True
            self.topics.append(t)
            self.event("nieuw_topic", t, regime=c["regime"], mmp=c["mmp"])
            self.backfill(t)
            self.observe([t])

    def backfill(self, t):
        try:
            counts = self.ctx.news.topic_counts(t["keywords"], self.day - timedelta(days=2),
                                                self.cfg["newsapi"]["backfill_days"])
        except Exception as e:  # noqa: BLE001
            self.error(f"basislijn {t['slug']}", e)
            return
        rows = []
        for day_s, cnt in counts.items():
            # telling van nieuwsdag d hoort bij rundag d+1
            run_day = _d(day_s) + timedelta(days=1)
            if run_day < self.day:
                rows.append({"topic_id": t["id"], "day": run_day, "status": t["status"], "articles": cnt,
                             "stale": True, "gates": {}, "notes": {"backfill": True}})
        self.repo.save_daily(rows)

    # --- 3. historie en koersen -----------------------------------------------------
    def load_history(self):
        since = self.day - timedelta(days=self.cfg["narrative"]["percentile_window_days"])
        rows = self.repo.history([t["id"] for t in self.topics], since)
        self.hist: dict[str, list[dict]] = {}
        for r in rows:
            if _d(r["day"]) < self.day:
                self.hist.setdefault(r["topic_id"], []).append(r)
        self.r_pop = self.repo.r_population(since)

    def load_market(self):
        syms = {self.ctx.bench["reference"], "EURUSD=X"}
        for t in self.topics:
            syms |= {b["symbol"] for b in t.get("benchmarks") or []}
        self.baskets = self.repo.active_baskets()
        for b in self.baskets.values():
            syms |= {m["symbol"] for m in b["members"]}
        syms |= set(self.ctx.bench["instruments"])
        try:
            self.ctx.market.load(sorted(syms), self.day)
        except Exception as e:  # noqa: BLE001
            self.error("koersen", e)

    # --- 4. narratief ------------------------------------------------------------------
    def update_narrative(self):
        n = self.cfg["narrative"]
        for t in self.topics:
            o = self.obs.get(t["id"], {})
            hl = n["shock_half_life_days"] if t["regime"] == "shock" else n["half_life_days"]
            if o.get("articles") == 0:
                x = 0.0
            else:
                x = scoring.narrative_input(o.get("articles"), o.get("alignment"))
            # Basis is de laatste R van vóór vandaag, zodat een tweede run op dezelfde dag niet dubbel telt.
            prev_rows = [h for h in self.hist.get(t["id"], []) if h.get("r_score") is not None]
            if prev_rows:
                base = prev_rows[-1]["r_score"]
            elif _d(t.get("first_seen")) == self.day:
                base = None
            else:
                base = t.get("r_score")
            r, stale = scoring.update_r(base, x, scoring.ema_lambda(hl))
            t["r_score"], t["_stale"] = r, stale
            base_since = self.day - timedelta(days=n["baseline_days"])
            hist_counts = [h.get("articles") for h in self.hist.get(t["id"], []) if _d(h["day"]) >= base_since]
            o["volume_z"] = scoring.volume_z(o.get("articles"), hist_counts, n["min_baseline_days"],
                                             n["volume_z_std_floor"])
            if o.get("tags"):
                t["meta"]["emerging_tags"] = o["tags"]

    # --- 5. reasoning -------------------------------------------------------------------
    def needs_reasoning(self, t) -> bool:
        if t["status"] == "active" or t.get("_approved_shock") or t.get("_new"):
            return True
        if not t.get("benchmarks") or not t.get("horizon_class"):
            return True  # eenmalig invullen, ook voor radar-topics (nodig voor de goedkeuringskaart en M/P)
        return (t.get("_i") or 0) >= self.cfg["on_deck"]["reasoning_min_i"]

    def mutation_check(self, t) -> bool:
        if t["status"] != "active" or t["regime"] != "shock" or not t.get("promoted_at"):
            return False
        days = (self.day - _d(t["promoted_at"])).days
        return days >= self.cfg["shock_radar"]["mutation_check_after_days"] and not t["meta"].get("mutation_done")

    def polymarket_context(self, t) -> list[dict]:
        markets = []
        for slug in t.get("polymarket_slugs") or []:
            m = self.ctx.poly.market(slug)
            if m:
                markets.append({**m, "linked": True})
        if len(markets) < 2:
            q = " ".join(t["keywords"][:2]) if t.get("keywords") else t["title"]
            markets += [{**m, "linked": False} for m in self.ctx.poly.search(q)
                        if m["slug"] not in {x["slug"] for x in markets}]
        return markets[:8]

    def reason(self):
        bench_list = {s: v["name"] for s, v in self.ctx.bench["instruments"].items()}
        schema = prompts.reasoning_schema(sorted(bench_list))
        others_all = [{"slug": t["slug"], "title": t["title"], "driver": t["main_macro_driver"], "status": t["status"]}
                      for t in self.topics]
        self.overlaps: list[tuple[str, str]] = []
        for t in [t for t in self.topics if self.needs_reasoning(t)]:
            try:
                self.reason_topic(t, bench_list, schema, others_all)
            except Exception as e:  # noqa: BLE001
                self.error(f"reasoning {t['slug']}", e)

    def reason_topic(self, t, bench_list, schema, others_all, second_pass: bool = False):
        o = self.obs.get(t["id"], {})
        markets = self.polymarket_context(t)
        last_change = _d(t.get("last_driver_change")) or _d(t.get("first_seen")) or self.day
        payload = {
            "topic": {k: t.get(k) for k in ("slug", "title", "main_macro_driver", "keywords", "regime", "status",
                                            "horizon_class", "version")},
            "needs_benchmarks": not t.get("benchmarks"),
            "mutation_check": self.mutation_check(t) and not second_pass,
            "driver_update_allowed": (self.day - last_change).days >= self.cfg["on_deck"]["driver_rewrite_min_days"]
                                     and not second_pass,
            "koppen_gisteren": o.get("headlines", []),
            "opkomende_deelthema's": t["meta"].get("emerging_tags", []),
            "polymarket": markets,
            "benchmark_lijst": bench_list,
            "andere_topics": [x for x in others_all if x["slug"] != t["slug"]],
        }
        res = self.ctx.llm.structured("reasoning", prompts.REASONING_SYSTEM, payload, "submit_reasoning", schema)
        hc = str(res.get("horizon_class", "")).lower()
        t["horizon_class"] = hc if hc in prompts.HORIZONS else t.get("horizon_class")
        t["meta"]["horizon_reason"] = res.get("horizon_reason")
        if payload["needs_benchmarks"]:
            canon = {k.upper(): k for k in bench_list}
            bms = [{"symbol": canon[str(b["symbol"]).upper()], "direction": 1 if int(b["direction"]) >= 0 else -1,
                    "why": b.get("why")}
                   for b in res.get("benchmarks", []) if str(b.get("symbol", "")).upper() in canon][:3]
            if bms:
                t["benchmarks"] = bms
                self.repo.add_version(t, None)
                self.ctx.market.load([b["symbol"] for b in bms], self.day)
        offered = {m["slug"] for m in markets}
        t["polymarket_slugs"] = [s for s in res.get("polymarket_slugs", []) if s in offered][:5]
        t["meta"]["resolved"] = bool(res.get("resolved"))
        t["meta"]["resolved_reason"] = res.get("resolved_reason")
        t["meta"]["polymarket"] = [m for m in markets if m["slug"] in t["polymarket_slugs"]]
        for slug in res.get("overlaps_with", []):
            if slug != t["slug"]:
                self.overlaps.append((t["slug"], slug))

        mut = res.get("mutation") or {}
        if payload["mutation_check"] and mut.get("mutate") and mut.get("new_driver"):
            t["meta"]["mutation_done"] = True
            t["regime"] = "structural"
            t["protected_until"] = None
            self.new_version(t, mut.get("new_title") or t["title"], mut["new_driver"],
                             mut.get("new_keywords") or t["keywords"], f"shock-mutatie: {mut.get('reason')}")
            self.event("shock_mutatie", t, reden=mut.get("reason"))
            self.reason_topic(t, bench_list, schema, others_all, second_pass=True)
            return
        upd = res.get("driver_update") or {}
        if payload["driver_update_allowed"] and upd.get("update") and upd.get("new_driver"):
            self.new_version(t, t["title"], upd["new_driver"], upd.get("new_keywords") or t["keywords"],
                             f"driver herschreven: {upd.get('reason')}")
            self.event("driver_update", t, reden=upd.get("reason"))
            self.reason_topic(t, bench_list, schema, others_all, second_pass=True)

    def new_version(self, t, title, driver, keywords, reason):
        t["version"] = int(t.get("version", 1)) + 1
        t["title"], t["main_macro_driver"] = title, driver
        t["keywords"] = list(dict.fromkeys(keywords))[:6]
        t["benchmarks"] = []  # vast per versie: opnieuw kiezen
        t["last_driver_change"] = self.day
        t["_version_changed"] = True
        self.repo.add_version(t, reason)

    # --- 6. score ----------------------------------------------------------------------
    def score_all(self):
        sc, ga = self.cfg["score"], self.cfg["gates"]
        ref_sym = self.ctx.bench["reference"]
        ref = self.ctx.market.close(ref_sym)
        pop = self.r_pop + [t.get("r_score") for t in self.topics]
        for t in self.topics:
            n = scoring.percentile_rank(t.get("r_score"), pop)
            z20s, zls = [], []
            for b in t.get("benchmarks") or []:
                close = self.ctx.market.close(b["symbol"])
                if close is None:
                    continue
                inst = self.ctx.bench["instruments"].get(b["symbol"], {})
                rel = ref if inst.get("relative_to") and b["symbol"] != ref_sym else None
                z20s.append(metrics.z_move(close, sc["m_window"], sc["vol_window"], rel, b["direction"]))
                zls.append(metrics.z_move(close, sc["p_window"], sc["vol_window"], rel, b["direction"]))
            m = scoring.m_component(z20s, sc["m_divisor"])
            p = scoring.p_component(zls, sc["p_z_low"], sc["p_z_high"])
            prev = (self.hist.get(t["id"]) or [{}])[-1]
            if m is None and t.get("benchmarks"):
                m = prev.get("m")  # koersdata ontbreekt: houd gisteren vast
            if p is None and t.get("benchmarks"):
                p = prev.get("p")
            d = scoring.d_component(t.get("horizon_class"), sc["horizon_values"])
            i = scoring.i_score(n, m, d, p, sc["component_floor"])
            o = self.obs.get(t["id"], {})
            window = [h.get("alignment") for h in self.hist.get(t["id"], [])
                      if _d(h["day"]) > self.day - timedelta(days=ga["alignment_window_days"])]
            window.append(o.get("alignment"))
            basket = self.baskets.get(t["id"]) if hasattr(self, "baskets") else None
            if t["status"] == "active":
                investable = (bool(basket) and len(basket["members"]) >= ga["min_tickers"]
                              and not t["meta"].get("basket_invalid"))
            else:
                investable = None
            exempt = t["regime"] == "shock" and (t.get("_approved_shock") or lifecycle.is_protected(t, self.day))
            gates = scoring.evaluate_gates(alignment_window=window, min_alignment=ga["min_alignment_7d"],
                                           horizon_class=t.get("horizon_class"), horizon_exempt=exempt,
                                           investable=investable, resolved=bool(t["meta"].get("resolved")))
            t["_i"], t["_gates"] = i, gates
            t["_comps"] = {"n": n, "m": m, "d": d, "p": p}

    # --- 7. shock-radar en merges --------------------------------------------------------
    def shock_radar(self):
        sr = self.cfg["shock_radar"]
        for t in self.tracked("radar"):
            o = self.obs.get(t["id"], {})
            existing = self.repo.approvals_for_topic(t["id"])
            age = (self.day - (_d(t.get("first_seen")) or self.day)).days
            open_or_used = [a for a in existing if a["status"] in ("pending", "approved", "applied")]
            if not existing:
                vz, al = o.get("volume_z"), o.get("alignment")
                spike = vz is not None and vz >= sr["volume_z_threshold"]
                if (t.get("high_impact") or spike) and al is not None and al >= sr["min_alignment"]:
                    vz_txt = f"volume z={vz:.1f}" if vz is not None else "volume z onbekend"
                    self.repo.create_approval({
                        "kind": "shock", "topic_id": t["id"],
                        "summary": f"{t['title']}: {vz_txt}, alignment {al:.2f}",
                        "payload": {"volume_z": vz, "alignment": al, "articles": o.get("articles"),
                                    "driver": t["main_macro_driver"], "headlines": o.get("headlines", [])[:6],
                                    "polymarket": self.polymarket_context(t)[:4], "i_score": t.get("_i"),
                                    "high_impact": t.get("high_impact")},
                        "expires_at": self.now + timedelta(hours=sr["approval_hours"]),
                    })
                    self.event("shock_goedkeuring_gevraagd", t, volume_z=vz)
                    continue
            if age >= sr["radar_max_days"] and not open_or_used and not t.get("_approved_shock"):
                t["status"], t["retired_at"] = "retired", self.day
                self.event("radar_verlopen", t)

    def merges(self):
        by_slug = {t["slug"]: t for t in self.topics}
        done = set()
        for a_slug, b_slug in getattr(self, "overlaps", []):
            a, b = by_slug.get(a_slug), by_slug.get(b_slug)
            key = tuple(sorted((a_slug, b_slug)))
            if not a or not b or key in done or a["status"] not in ("radar", "on_deck", "active") \
                    or b["status"] not in ("radar", "on_deck", "active"):
                continue
            done.add(key)
            if "active" not in (a["status"], b["status"]):
                keep, absorb = sorted((a, b), key=lambda t: t.get("r_score") or 0, reverse=True)
                self.merge_topics(keep, absorb, "automatische merge (overlap)")
                continue
            if a["status"] == "active" and b["status"] == "active":
                keep, absorb = sorted((a, b), key=lambda t: t.get("_i") or 0, reverse=True)
            else:
                keep, absorb = (a, b) if a["status"] == "active" else (b, a)
            pending = [x for x in self.repo.approvals(["pending"]) if x["kind"] == "merge"
                       and {x["topic_id"], x.get("other_topic_id")} == {keep["id"], absorb["id"]}]
            if not pending:
                self.repo.create_approval({
                    "kind": "merge", "topic_id": keep["id"], "other_topic_id": absorb["id"],
                    "summary": f"Samenvoegen: '{absorb['title']}' in '{keep['title']}' (dubbeltelling)",
                    "payload": {"keep": keep["slug"], "absorb": absorb["slug"]},
                    "expires_at": self.now + timedelta(hours=self.cfg["shock_radar"]["approval_hours"]),
                })
                self.event("merge_goedkeuring_gevraagd", keep, absorb=absorb["slug"])

    def merge_topics(self, keep, absorb, reason):
        keep["keywords"] = list(dict.fromkeys((keep.get("keywords") or []) + (absorb.get("keywords") or [])))[:8]
        keep["source_event_uris"] = list(dict.fromkeys((keep.get("source_event_uris") or [])
                                                       + (absorb.get("source_event_uris") or [])))[:20]
        keep["version"] = int(keep.get("version", 1)) + 1
        self.repo.add_version(keep, f"{reason}: {absorb['slug']}")
        if absorb["status"] == "active":
            self.repo.set_basket_status(absorb["id"], "exit")
        absorb["status"], absorb["merged_into"], absorb["retired_at"] = "merged", keep["id"], self.day
        self.event("merge", keep, absorb=absorb["slug"], reden=reason)

    # --- 8. levenscyclus -------------------------------------------------------------
    def lifecycle(self):
        keys = ("streak_in", "gate_fail_streak", "low_i_streak")
        for t in self.topics:
            if t["status"] in ("radar", "on_deck", "active"):
                # Idempotent per dag: bij een herhaalde run starten we vanaf de tellers van vóór vandaag.
                if t["meta"].get("streak_day") == self.day.isoformat():
                    t.update(t["meta"].get("streak_base", {}))
                else:
                    t["meta"]["streak_base"] = {k: t.get(k, 0) for k in keys}
                    t["meta"]["streak_day"] = self.day.isoformat()
                lifecycle.update_streaks(t, t.get("_i"), t["_gates"]["pass"], self.cfg)

        incumbents = []
        for t in self.tracked("active"):
            reason = lifecycle.should_exit(t, t.get("_i"), self.day, self.cfg)
            if reason:
                self.demote(t, reason)
            else:
                incumbents.append(t)

        approved = [t for t in self.topics if t.get("_approved_shock") and t["status"] in ("radar", "on_deck")]
        for t in approved:
            g = t["_gates"]
            if not (g["thesis"] and g["unresolved"]):
                self.finish_approval(t, "gate thesis of resolutie faalt")
                t["_approved_shock"] = False
        approved = [t for t in approved if t.get("_approved_shock")]
        enter_days = self.cfg["active"]["enter_days"]
        cands = [t for t in self.tracked("on_deck") if t["streak_in"] >= enter_days and not t.get("_approved_shock")]
        cands.sort(key=lambda t: t.get("_i") or 0, reverse=True)

        current, displaced = lifecycle.select_active(incumbents, approved + cands, self.cfg, self.try_enter, self.day)
        for t in displaced:
            self.demote(t, "verdrongen door sterker topic")
        for t in approved:
            if t["status"] != "active":
                self.finish_approval(t, "geen plek of mandje ongeldig")

        self.refresh_baskets()
        self.purge()

    def try_enter(self, t) -> bool:
        ok = self.ensure_basket(t, "promotie")
        if not ok:
            t["_gates"]["investable"] = False
            t["_gates"]["pass"] = False
            self.event("promotie_geblokkeerd", t, reden="minder dan 4 geldige tickers")
            return False
        t["status"], t["promoted_at"], t["demoted_at"] = "active", self.day, None
        t["_gates"]["investable"] = True
        self.event("promotie", t, i=round(t.get("_i") or 0, 1), shock=bool(t.get("_approved_shock")))
        if t.get("_approved_shock"):
            self.finish_approval(t, "toegelaten als Active")
        return True

    def finish_approval(self, t, note):
        aid = t.get("_approval_id")
        if aid:
            self.repo.set_approval(aid, {"status": "applied", "note": note})

    def demote(self, t, reason):
        t["status"], t["demoted_at"], t["protected_until"] = "on_deck", self.day, None
        t["streak_in"] = 0
        self.repo.set_basket_status(t["id"], "exit")
        self.baskets.pop(t["id"], None)
        self.event("uitstap", t, reden=reason)

    def ensure_basket(self, t, reason) -> bool:
        b = self.baskets.get(t["id"])
        if b and int(b["topic_version"]) == int(t.get("version", 1)) and not t.get("_force_search"):
            return len(b["members"]) >= self.cfg["stock_search"]["min_tickers"]
        if self.stock_searches >= self.cfg["stock_search"]["max_per_run"]:
            self.error(f"stock search {t['slug']}", RuntimeError("maximum zoekrondes per run bereikt"))
            return bool(b) and len(b["members"]) >= self.cfg["stock_search"]["min_tickers"]
        taken = {m["symbol"] for tid, bb in self.baskets.items() if tid != t["id"] for m in bb["members"]}
        self.stock_searches += 1
        try:
            res = stock_search.run_stock_search(t, self.ctx.llm, self.ctx.market, taken, self.cfg, self.day)
        except Exception as e:  # noqa: BLE001
            self.error(f"stock search {t['slug']}", e)
            return bool(b) and len(b["members"]) >= self.cfg["stock_search"]["min_tickers"]
        if not res["valid"]:
            t["meta"]["last_stock_search"] = {"day": self.day.isoformat(), **res["notes"]}
            return False
        t["meta"].pop("basket_invalid", None)
        notes = {**res["notes"], "day": self.day.isoformat()}
        self.baskets[t["id"]] = self.repo.save_basket(t, reason, res["members"], res["cash"], notes)
        t["meta"]["last_stock_search"] = {"day": self.day.isoformat(), "valid": True}
        self.event("mandje", t, reden=reason, tickers=[m["symbol"] for m in res["members"]])
        return True

    def refresh_baskets(self):
        """Ontbrekend mandje, nieuwe topic-versie, of maandelijkse review (mandje ouder dan 30 dagen).
        De kostenrem (max_per_run) spreidt reviews vanzelf over meerdere dagen."""
        for t in self.tracked("active"):
            if t.get("promoted_at") and _d(t["promoted_at"]) == self.day:
                continue
            b = self.baskets.get(t["id"])
            if b is None:
                self.ensure_basket(t, "ontbrekend mandje")
            elif int(b["topic_version"]) != int(t.get("version", 1)) or t.get("_version_changed"):
                self.ensure_basket(t, "nieuwe topic-versie")
                nb = self.baskets.get(t["id"])
                if nb and int(nb["topic_version"]) == int(t.get("version", 1)):
                    t["meta"].pop("basket_invalid", None)
                elif (t["meta"].get("last_stock_search") or {}).get("day") == self.day.isoformat():
                    # Zoekronde voor de nieuwe these mislukt: gate 3 faalt tot een ronde wel slaagt.
                    t["meta"]["basket_invalid"] = self.day.isoformat()
            elif (self.day - _d((b.get("notes") or {}).get("day") or b.get("created_at") or self.day)).days >= 30:
                t["_force_search"] = True
                self.ensure_basket(t, "maandelijkse review")
                t["_force_search"] = False

    def purge(self):
        od = self.cfg["on_deck"]
        for t in self.tracked("on_deck"):
            if t.get("low_i_streak", 0) >= od["purge_after_days"]:
                t["status"], t["retired_at"] = "retired", self.day
                self.event("opgeruimd", t, reden=f"I < {od['purge_below_i']} gedurende {od['purge_after_days']} dagen")
        on_deck = self.tracked("on_deck")
        if len(on_deck) > od["max_topics"]:
            old = [t for t in on_deck if (self.day - (_d(t.get("first_seen")) or self.day)).days >= 3]
            old.sort(key=lambda t: t.get("r_score") or 0)
            for t in old[: len(on_deck) - od["max_topics"]]:
                t["status"], t["retired_at"] = "retired", self.day
                self.event("opgeruimd", t, reden="On Deck vol")

    # --- 9. allocatie -------------------------------------------------------------------
    def allocate(self):
        from . import weighting

        active = self.tracked("active")
        topic_w, cash = lifecycle.topic_weights(active, self.cfg)
        baskets = {tid: {m["symbol"]: m["weight"] for m in b["members"]}
                   for tid, b in self.baskets.items() if tid in topic_w}
        addv_eur, currency = {}, {}
        for b in self.baskets.values():
            for m in b["members"]:
                s = m["symbol"]
                currency[s] = m.get("currency") or self.ctx.market.currency(s)
                c, v = self.ctx.market.close(s), self.ctx.market.volume(s)
                a = metrics.addv(c, v, 20) if c is not None and v is not None else None
                fx = self.ctx.market.fx_to_eur(currency[s]) if a else None
                if a and fx:
                    addv_eur[s] = a * fx
        proposed, _ = weighting.portfolio_targets(topic_w, baskets, addv_eur, self.cfg)
        last = self.repo.last_snapshot(self.day)
        last_targets = {x["symbol"]: x for x in (last or {}).get("targets", [])}
        basket_ids_now = sorted({b["id"] for tid, b in self.baskets.items() if tid in topic_w})
        basket_ids_last = sorted({bid for x in last_targets.values() for bid in x.get("basket_ids", [])})
        composition_changed = last is not None and basket_ids_now != basket_ids_last
        prices_now = {s: self.ctx.market.last_price(s) for s in set(last_targets) | set(proposed)}
        drift = weighting.implied_drift({s: x["weight"] for s, x in last_targets.items()},
                                        {s: x.get("price") for s, x in last_targets.items()}, prices_now)
        rebalance, reason = weighting.needs_rebalance(last, self.day, composition_changed, drift, self.cfg)
        if not proposed and not last_targets:
            rebalance, reason = False, None
        sym_meta = {}
        for tid, b in self.baskets.items():
            for m in b["members"]:
                sym_meta.setdefault(m["symbol"], {"name": m.get("name"), "tier": m["tier"], "basket_ids": []})
                if tid in topic_w:
                    sym_meta[m["symbol"]]["basket_ids"].append(b["id"])
        if rebalance:
            targets = [{"symbol": s, "weight": round(e["weight"], 6), "topic_ids": e["topics"],
                        "basket_ids": sym_meta.get(s, {}).get("basket_ids", []), "price": prices_now.get(s),
                        "currency": currency.get(s), "name": sym_meta.get(s, {}).get("name"),
                        "tier": sym_meta.get(s, {}).get("tier"), "capped": "capped_from" in e}
                       for s, e in sorted(proposed.items(), key=lambda kv: -kv[1]["weight"])]
            last_reb = self.day
            self.event("rebalance", None, reden=reason)
        else:
            targets = list(last_targets.values())
            last_reb = _d(last["last_rebalance_day"]) if last and last.get("last_rebalance_day") else None
        target_cash = max(0.0, 1.0 - sum(x["weight"] for x in targets))
        cur_exp: dict[str, float] = {}
        for x in targets:
            cur_exp[x.get("currency") or "?"] = cur_exp.get(x.get("currency") or "?", 0) + x["weight"]
        cur_exp["CASH (EUR)"] = target_cash
        self.snapshot = {
            "day": self.day,
            "topic_weights": [{"topic_id": t["id"], "slug": t["slug"], "title": t["title"],
                               "weight": round(topic_w.get(t["id"], 0), 6), "i_score": t.get("_i"),
                               "regime": t["regime"]} for t in sorted(active, key=lambda t: -(t.get("_i") or 0))],
            "cash": round(cash, 6), "targets": targets, "target_cash": round(target_cash, 6),
            "is_rebalance": rebalance, "rebalance_reason": reason, "last_rebalance_day": last_reb,
            "drift_max": round(drift, 6), "currency_exposure": {k: round(v, 6) for k, v in cur_exp.items()},
            "heat_index": _mean([t.get("_i") for t in active]) or 0.0,
            "portfolio_value_eur": self.cfg["weighting"]["portfolio_value_eur"],
        }

    # --- 9b. papieren handel ---------------------------------------------------------------
    def paper_trading(self):
        from . import paper

        mk = self.ctx.market
        start_value = float(self.cfg["weighting"]["portfolio_value_eur"])
        last = self.repo.last_snapshot(self.day)
        lp = (last or {}).get("paper") or {}
        closing = self.repo.unclosed_trades()
        currency = dict(lp.get("currency", {}))
        for b in list(self.baskets.values()) + closing:
            for m in b["members"]:
                currency[m["symbol"]] = m.get("currency") or currency.get(m["symbol"]) or "USD"
        for x in self.snapshot["targets"]:
            currency[x["symbol"]] = x.get("currency") or currency.get(x["symbol"]) or "USD"
        ref = self.ctx.bench["reference"]
        mk.load(sorted(set(currency) | {ref}), self.day)
        prices = {s: mk.last_price(s) for s in currency}
        fxs = {s: mk.fx_to_eur(c) for s, c in currency.items()}
        bp, bf = mk.last_price(ref), mk.fx_to_eur("USD")
        bench_eur = bp * bf if bp and bf else None
        ref_close = mk.close(ref)
        ref_close = ref_close.dropna() if ref_close is not None else None
        price_day = ref_close.index[-1].date().isoformat() if ref_close is not None and len(ref_close) else None

        # 1. Uitstap: mandjes die vandaag (of eerder) zijn beëindigd, krijgen hun uitstapkoers.
        for b in closing:
            for m in b["members"]:
                self.repo.update_member(b["id"], m["symbol"], {"exit_price": prices.get(m["symbol"]),
                                                               "exit_fx": fxs.get(m["symbol"])})
            perf = paper.basket_perf(b["members"], prices, fxs, (b.get("perf") or {}).get("bench_entry_eur"),
                                     bench_eur)
            self.repo.update_basket(b["id"], {"exit_day": self.day,
                                              "perf": {**perf, "as_of": self.day.isoformat(),
                                                       "price_day": price_day, "closed": True}})
            self.event("mandje_gesloten", None, basket=b["id"], rendement=round(perf["return_eur"] * 100, 2))

        # 2. Instap en dagelijkse stand van de actieve mandjes in de doelportefeuille.
        in_targets = {bid for x in self.snapshot["targets"] for bid in x.get("basket_ids", [])}
        for b in self.baskets.values():
            if b["id"] not in in_targets:
                continue
            perf_old = b.get("perf") or {}
            # Ook bij een herhaalde run op de instapdag opnieuw vastleggen: de papieren portefeuille wordt
            # die dag met dezelfde koersen opnieuw verdeeld, zo blijven mandje en portefeuille gelijk.
            if not b.get("entry_day") or str(b["entry_day"])[:10] == self.day.isoformat():
                for m in b["members"]:
                    m["entry_price"], m["entry_fx"] = prices.get(m["symbol"]), fxs.get(m["symbol"])
                    self.repo.update_member(b["id"], m["symbol"], {"entry_price": m["entry_price"],
                                                                   "entry_fx": m["entry_fx"]})
                b["entry_day"] = self.day
                perf_old = {"bench_entry_eur": bench_eur}
            perf = paper.basket_perf(b["members"], prices, fxs, perf_old.get("bench_entry_eur"), bench_eur)
            b["perf"] = {**perf, "as_of": self.day.isoformat(), "price_day": price_day}
            self.repo.update_basket(b["id"], {"entry_day": b["entry_day"], "perf": b["perf"]})

        # 3. Papieren portefeuille: dagelijks herwaarderen, bij een rebalance opnieuw verdelen.
        holdings, cash = dict(lp.get("holdings", {})), float(lp.get("cash", start_value))
        nav, values = paper.mark_to_market(holdings, cash, prices, fxs, lp.get("values", {}))
        bench_units, start_day = lp.get("bench_units"), lp.get("start_day")
        if self.snapshot["is_rebalance"] and self.snapshot["targets"]:
            holdings, cash, values = paper.rebalance(nav, self.snapshot["targets"], prices, fxs)
            if bench_units is None and bench_eur:
                bench_units, start_day = nav / bench_eur, self.day.isoformat()
        bench_nav = bench_units * bench_eur if bench_units and bench_eur else lp.get("bench_nav", start_value)
        self.snapshot["paper"] = {
            "nav": nav, "cash": cash, "holdings": holdings, "values": values,
            "currency": {s: currency[s] for s in holdings if s in currency},
            "start_value": start_value, "start_day": start_day, "price_day": price_day,
            "return": nav / start_value - 1.0,
            "bench_units": bench_units, "bench_nav": bench_nav,
            "bench_return": (bench_nav / start_value - 1.0) if start_day else None,
        }

    # --- 10. opslaan ----------------------------------------------------------------------
    def persist(self):
        rows = []
        for t in self.topics:
            o = self.obs.get(t["id"], {})
            c = t.get("_comps", {})
            g = t.get("_gates", {})
            rows.append({
                "topic_id": t["id"], "day": self.day, "status": t["status"], "articles": o.get("articles"),
                "volume_z": o.get("volume_z"), "sentiment": o.get("sentiment"), "alignment": o.get("alignment"),
                "backward_looking": o.get("backward"), "r_score": t.get("r_score"), "n": c.get("n"),
                "m": c.get("m"), "d": c.get("d"), "p": c.get("p"), "i_score": t.get("_i"), "gates": g,
                "gates_pass": g.get("pass"), "stale": bool(t.get("_stale")),
                "notes": {"tags": o.get("tags", []), "headlines": o.get("headlines", [])[:5]},
            })
            self.repo.save_topic(t)
        self.repo.save_daily(rows)
        self.repo.save_snapshot(self.snapshot)
