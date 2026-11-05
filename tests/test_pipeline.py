"""End-to-end over een synthetische maand, plus parsing van de externe API's."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from engine.fakes import FakeLLM, FakeMarket, FakeNews, FakePoly, FakeWorld
from engine.newsapi import NewsApiClient, TokenBudgetExceeded
from engine.pipeline import Context, DailyRun
from engine.polymarket import normalize_market
from engine.repo import Repo
from engine.settings import load_benchmarks, load_settings
from engine.store import MemoryStore, SupabaseStore

CFG, BENCH = load_settings(), load_benchmarks()


def simulate(days=30, approve=True):
    start = date(2026, 10, 1)
    world, store = FakeWorld(start), MemoryStore()
    repo, market, llm = Repo(store), FakeMarket(world), FakeLLM()
    results = []
    for k in range(days):
        day = start + timedelta(days=k)
        if approve:
            for a in repo.approvals(["pending"]):
                repo.set_approval(a["id"], {"status": "approved", "decided_by": "test"})
        ctx = Context(cfg=CFG, bench=BENCH, repo=repo, news=FakeNews(world), poly=FakePoly(), market=market,
                      llm=llm, dry_run=True)
        results.append(DailyRun(ctx, day).run())
    return store, results


@pytest.fixture(scope="module")
def sim():
    return simulate()


def kinds(results, kind):
    return [e for r in results for e in r["events"] if e["kind"] == kind]


def test_all_runs_succeed(sim):
    _, results = sim
    assert all(r["status"] == "ok" for r in results), [r["errors"] for r in results if r["errors"]]


def test_noise_filtered_and_short_horizon_never_active(sim):
    store, _ = sim
    titles = {t["title"]: t for t in store.select("topics")}
    assert "Celebrity wedding" not in titles
    ecb = titles["ECB-renteverlaging december"]
    assert ecb["status"] == "on_deck"
    assert not store.select("baskets", [("topic_id", "eq", ecb["id"])])


def test_promotion_needs_three_days(sim):
    store, results = sim
    promos = kinds(results, "promotie")
    ai = next(e for e in promos if e["slug"] == "ai-stroom-en-netcapaciteit")
    first = store.select("topics", [("slug", "eq", ai["slug"])])[0]["first_seen"]
    day_idx = next(i for i, r in enumerate(results) if ai in r["events"])
    assert day_idx - (date.fromisoformat(first) - date(2026, 10, 1)).days == CFG["active"]["enter_days"] - 1


def test_shock_flow_approval_entry_and_mutation(sim):
    store, results = sim
    assert kinds(results, "shock_goedkeuring_gevraagd")
    shock_promo = [e for e in kinds(results, "promotie") if e.get("shock")]
    assert shock_promo
    mut = kinds(results, "shock_mutatie")
    assert mut and mut[0]["topic"] == "Tankervracht en olie-risicopremie"
    t = store.select("topics", [("slug", "eq", shock_promo[0]["slug"])])[0]
    assert t["regime"] == "structural" and t["version"] == 2 and t["status"] == "active"
    reasons = [v["reason"] for v in store.select("topic_versions", [("topic_id", "eq", t["id"])])]
    assert any("shock-mutatie" in (r or "") for r in reasons)
    assert store.select("approvals")[0]["status"] == "applied"


def test_baskets_filter_and_weights(sim):
    store, _ = sim
    for b in store.select("baskets", [("status", "eq", "active")]):
        ms = store.select("basket_members", [("basket_id", "eq", b["id"])])
        syms = {m["symbol"] for m in ms}
        assert 4 <= len(ms) <= 8
        assert "NEWCO" not in syms and "ILLIQ" not in syms
        assert sum(m["weight"] for m in ms) + b["cash_share"] == pytest.approx(1.0, abs=1e-4)
        assert max(m["weight"] for m in ms) <= CFG["weighting"]["max_weight_in_basket"] + 1e-6
        for m in ms:
            if m["pre_revenue"]:
                assert m["weight"] <= CFG["weighting"]["pre_revenue_max_weight"] + 1e-6


def test_snapshot_consistency(sim):
    store, _ = sim
    snaps = sorted(store.select("portfolio_snapshots"), key=lambda s: s["day"])
    for s in snaps:
        tw = sum(x["weight"] for x in s["topic_weights"])
        assert tw + s["cash"] == pytest.approx(1.0, abs=1e-5)
        assert all(x["weight"] <= CFG["active"]["topic_cap"] + 1e-9 for x in s["topic_weights"])
        assert sum(x["weight"] for x in s["targets"]) + s["target_cash"] == pytest.approx(1.0, abs=1e-4)
        assert all(x["weight"] <= CFG["weighting"]["max_weight_portfolio"] + 1e-9 for x in s["targets"])
        assert len(s["topic_weights"]) <= CFG["active"]["max_topics"]
    assert snaps[0]["targets"] == [] and snaps[-1]["targets"]
    assert any(s["is_rebalance"] for s in snaps)


def test_no_approval_means_shock_never_enters():
    store, results = simulate(days=16, approve=False)
    assert not [e for e in kinds(results, "promotie") if e.get("shock")]
    expired = [a for a in store.select("approvals")]
    assert expired and all(a["status"] in ("pending", "expired") for a in expired)


# --- externe API-parsing -----------------------------------------------------------------

def _news_client(handler, used=0.0):
    counters = {"t": used, "m": used}

    def record(c):
        counters["t"] += c
        counters["m"] += c

    c = NewsApiClient("k", CFG, lambda: counters["t"], lambda: counters["m"], record)
    c.http = httpx.Client(base_url=CFG["newsapi"]["base_url"], transport=httpx.MockTransport(handler))
    return c, counters


def test_newsapi_parsing_and_budget():
    def handler(req):
        body = json.loads(req.content)
        assert body["apiKey"] == "k"
        if req.url.path.endswith("/event/getEvents"):
            return httpx.Response(200, json={"events": {"results": [{
                "uri": "e1", "title": {"eng": "Event"}, "summary": {"eng": "Sum"}, "totalArticleCount": 50,
                "concepts": [{"label": {"eng": "Oil"}, "uri": "u"}], "categories": []}]}})
        if body.get("resultType") == "timeAggr":
            return httpx.Response(200, json={"timeAggr": {"results": [{"date": "2026-10-01", "count": 7}]}})
        return httpx.Response(200, json={"articles": {"totalResults": 123, "results": [
            {"title": "T", "body": "B" * 1000, "url": "u", "sentiment": 0.1, "source": {"title": "S"}}]}})

    c, counters = _news_client(handler)
    res = c.topic_articles(["Strait of Hormuz"], date(2026, 11, 3))
    assert res["total"] == 123 and len(res["articles"][0]["body"]) == 400
    assert c.discovery_events(date(2026, 11, 3))[0]["concepts"] == ["Oil"]
    assert c.topic_counts(["x"], date(2026, 11, 3), 30) == {"2026-10-01": 7}
    assert counters["t"] == 11
    c2, _ = _news_client(handler, used=CFG["newsapi"]["daily_token_limit"])
    with pytest.raises(TokenBudgetExceeded):
        c2.topic_articles(["x"], date(2026, 11, 3))


def test_polymarket_normalize():
    m = normalize_market({"slug": "s", "question": "Q", "outcomes": '["Yes","No"]', "outcomePrices": '["0.62","0.38"]',
                          "liquidityNum": 50000, "endDate": "2027-01-31T00:00:00Z", "closed": False})
    assert m["prob_yes"] == pytest.approx(0.62) and m["liquidity"] == 50000 and m["end_date"] == "2027-01-31"


def test_supabase_store_requests():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=[])

    s = SupabaseStore("https://x.supabase.co", "sb_secret_abc")
    s.client = httpx.Client(base_url="https://x.supabase.co/rest/v1", headers=s.client.headers,
                            transport=httpx.MockTransport(handler))
    s.select("topics", [("status", "in", ["active", "on_deck"]), ("day", "gte", date(2026, 1, 2))],
             order="day.desc", limit=5)
    q = str(seen[0].url)
    assert "status=in.%28%22active%22%2C%22on_deck%22%29" in q and "day=gte.2026-01-02" in q and "limit=5" in q
    assert seen[0].headers["apikey"] == "sb_secret_abc" and "authorization" not in seen[0].headers
    s.upsert("topic_daily", [{"a": 1, "b": 2}, {"a": 2}], "a")
    assert len(seen) == 3  # twee batches door ongelijke sleutels
    assert seen[1].headers["prefer"].startswith("resolution=merge-duplicates")


def test_market_parses_yfinance_multiindex(monkeypatch):
    import numpy as np
    import pandas as pd
    import yfinance

    from engine.market import MarketData

    idx = pd.date_range("2026-01-01", periods=5, tz="America/New_York")
    cols = pd.MultiIndex.from_product([["Close", "Volume"], ["XLU", "BZ=F"]], names=["Price", "Ticker"])
    df = pd.DataFrame(np.arange(20, dtype=float).reshape(5, 4) + 1, index=idx, columns=cols)
    monkeypatch.setattr(yfinance, "download", lambda *a, **k: df)
    m = MarketData()
    m.load(["XLU", "BZ=F"], date(2026, 1, 10))
    assert m.last_price("XLU") == df["Close"]["XLU"].iloc[-1]
    assert m.close("BZ=F").index.tz is None and len(m.volume("XLU")) == 5
    assert m.close("ONBEKEND") is None


def test_rerun_same_day_is_idempotent():
    start = date(2026, 10, 1)
    world, store = FakeWorld(start), MemoryStore()
    repo, market, llm = Repo(store), FakeMarket(world), FakeLLM()

    def run(day):
        ctx = Context(cfg=CFG, bench=BENCH, repo=repo, news=FakeNews(world), poly=FakePoly(), market=market,
                      llm=llm, dry_run=True)
        return DailyRun(ctx, day).run()

    for k in range(4):
        run(start + timedelta(days=k))
    before = {t["slug"]: (t["r_score"], t["streak_in"], t["status"]) for t in store.select("topics")}
    run(start + timedelta(days=3))  # zelfde dag nog eens
    after = {t["slug"]: (t["r_score"], t["streak_in"], t["status"]) for t in store.select("topics")}
    assert before == after


def test_build_members_uses_search_exposure_when_selection_omits_it():
    from engine.stock_search import build_members

    chosen = [{"symbol": "STNG", "exposure_pct": None, "pre_revenue": False, "rationale": "x", "subniche": "tankers"}]
    by_sym = {"STNG": {"symbol": "STNG", "name": "Scorpio", "role": "pure_play", "exposure_pct": 90,
                       "pre_revenue": False, "sigma": 0.4}}
    m = build_members(chosen, by_sym, CFG)[0]
    assert m["exposure_pct"] == 90 and m["tier"] == "pure_play"
    by_sym["STNG"]["exposure_pct"] = None
    assert build_members(chosen, by_sym, CFG)[0]["tier"] == "pure_play"  # rol als terugval


def test_skip_if_done():
    from engine.__main__ import already_ran

    repo = Repo(MemoryStore())
    day = date(2026, 11, 5)
    assert not already_ran(repo, day)
    rec = repo.start_run(day, False)
    assert not already_ran(repo, day)          # nog bezig telt niet
    repo.finish_run(rec["id"], {"status": "failed"})
    assert not already_ran(repo, day)          # mislukt telt niet
    rec = repo.start_run(day, False)
    repo.finish_run(rec["id"], {"status": "partial"})
    assert already_ran(repo, day)


def test_paper_trading_entries_exits_and_nav(sim):
    store, _ = sim
    baskets = store.select("baskets")
    opened = [b for b in baskets if b.get("entry_day")]
    assert opened
    for b in opened:
        ms = store.select("basket_members", [("basket_id", "eq", b["id"])])
        assert all(m.get("entry_price") and m.get("entry_fx") for m in ms)
        assert "return_eur" in b["perf"] and b["perf"]["bench_eur"] is not None
    closed = [b for b in baskets if b.get("exit_day")]
    assert closed, "in de simulatie wordt minstens één mandje vervangen of verlaten"
    for b in closed:
        assert b["status"] in ("exit", "archived") and b["perf"].get("closed")
        assert b["exit_day"] >= b["entry_day"]
        ms = store.select("basket_members", [("basket_id", "eq", b["id"])])
        assert all(m.get("exit_price") for m in ms)
    snaps = sorted(store.select("portfolio_snapshots"), key=lambda s: s["day"])
    assert snaps[0]["paper"]["nav"] == pytest.approx(CFG["weighting"]["portfolio_value_eur"])
    last = snaps[-1]["paper"]
    assert last["start_day"] and last["nav"] != pytest.approx(CFG["weighting"]["portfolio_value_eur"])
    assert last["nav"] == pytest.approx(last["cash"] + sum(last["values"].values()))
    assert last["bench_return"] is not None


def test_drop_incomplete_bar_during_us_market_hours():
    import pandas as pd

    from engine.market import drop_incomplete_bar

    df = pd.DataFrame({"X": [1.0, 2.0, 3.0]}, index=pd.to_datetime(["2026-11-03", "2026-11-04", "2026-11-05"]))
    during = datetime(2026, 11, 5, 19, 30, tzinfo=timezone.utc)    # 14:30 in New York
    after = datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc)      # 17:00 in New York
    morning = datetime(2026, 11, 6, 5, 17, tzinfo=timezone.utc)    # vóór opening, gisteren is afgesloten
    assert list(drop_incomplete_bar(df, during)["X"]) == [1.0, 2.0]
    assert len(drop_incomplete_bar(df, after)) == 3
    assert len(drop_incomplete_bar(df, morning)) == 3


def test_paper_nav_matches_basket_return_after_entry():
    """Instap en papieren portefeuille op dezelfde koersen: de winst van de portefeuille is
    precies topicgewicht x rendement van het mandje (zonder caps)."""
    from engine import paper

    members = [{"symbol": "A", "weight": 0.6}, {"symbol": "B", "weight": 0.4}]
    p0, p1 = {"A": 100.0, "B": 50.0}, {"A": 112.0, "B": 45.0}
    fx0, fx1 = {"A": 0.90, "B": 0.90}, {"A": 0.92, "B": 0.92}
    for m in members:
        m["entry_price"], m["entry_fx"] = p0[m["symbol"]], fx0[m["symbol"]]
    topic_w = 0.25
    targets = [{"symbol": m["symbol"], "weight": topic_w * m["weight"]} for m in members]
    holdings, cash, _ = paper.rebalance(100_000, targets, p0, fx0)
    nav, _ = paper.mark_to_market(holdings, cash, p1, fx1, {})
    perf = paper.basket_perf(members, p1, fx1, None, None)
    assert nav / 100_000 - 1 == pytest.approx(topic_w * perf["return_eur"], abs=1e-12)
