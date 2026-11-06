"""CLI.

  python -m engine run                 dagelijkse run (GitHub Actions)
  python -m engine run --day 2026-11-05
  python -m engine check               controleert sleutels en verbindingen (kost 0 nieuws-tokens)
  python -m engine simulate --days 25  synthetische run, zonder sleutels; schrijft dashboard/demo-data.json
  python -m engine add-user --email x@y.nl --name "Naam" [--role viewer]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import string
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .pipeline import Context, DailyRun
from .repo import Repo
from .settings import ROOT, load_benchmarks, load_settings
from .store import MemoryStore, SupabaseStore

log = logging.getLogger("engine")


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Ontbrekende omgevingsvariabele: {name}")
    return v


def supabase_repo() -> Repo:
    return Repo(SupabaseStore(env("SUPABASE_URL"), env("SUPABASE_SECRET_KEY")))


def build_live_context(cfg, bench, repo: Repo, day: date) -> Context:
    from .llm import LLM
    from .market import MarketData
    from .newsapi import NewsApiClient
    from .polymarket import PolymarketClient

    month_start = day.replace(day=1)
    counters = {"today": repo.usage("newsapi", day, day), "month": repo.usage("newsapi", month_start)}

    def record(cost: float):
        counters["today"] += cost
        counters["month"] += cost
        repo.add_usage(day, "newsapi", cost)

    news = NewsApiClient(env("NEWSAPI_KEY"), cfg, lambda: counters["today"], lambda: counters["month"], record)
    return Context(cfg=cfg, bench=bench, repo=repo, news=news, poly=PolymarketClient(cfg),
                   market=MarketData(repo, live_prices=cfg.get("market", {}).get("live_prices", True)),
                   llm=LLM(env("ANTHROPIC_API_KEY"), cfg),
                   now=datetime.now(timezone.utc))


def already_ran(repo: Repo, day: date) -> bool:
    rows = repo.s.select("runs", [("run_day", "eq", day), ("dry_run", "eq", False), ("status", "in", ["ok", "partial"])],
                        limit=1)
    return bool(rows)


def cmd_run(args, cfg, bench):
    day = date.fromisoformat(args.day) if args.day else datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    repo = supabase_repo()
    if args.skip_if_done and already_ran(repo, day):
        print(f"Run voor {day} is al gedaan; reserve-tijdstip slaat over.")
        return 0
    ctx = build_live_context(cfg, bench, repo, day)
    result = DailyRun(ctx, day).run()
    print(json.dumps({"day": day.isoformat(), **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] in ("ok", "partial") else 1


def cmd_check(args, cfg, bench):
    import httpx

    ok = True
    repo = supabase_repo()
    try:
        repo.s.select("topics", limit=1)
        print("Supabase: ok")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Supabase: FOUT {e}")
    try:
        r = httpx.post(cfg["newsapi"]["base_url"] + "/usage", json={"apiKey": env("NEWSAPI_KEY")}, timeout=30)
        d = r.json()
        print(f"newsAPI.ai: ok, {d.get('usedTokens')} van {d.get('availableTokens')} tokens gebruikt")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"newsAPI.ai: FOUT {e}")
    try:
        from .llm import LLM

        llm = LLM(env("ANTHROPIC_API_KEY"), cfg)
        for key in ("lite", "reasoning"):
            out = llm.structured(key, "Antwoord in het schema.", "Zeg ok.", "ok",
                                 {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                                 max_tokens=500)
            print(f"Anthropic: ok ({cfg['models'][key]}) -> {out}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Anthropic: FOUT {e}")
    try:
        from .market import MarketData

        m = MarketData()
        m.load(["ACWI"], date.today())
        print(f"yfinance: ok, ACWI {m.last_price('ACWI')}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"yfinance: FOUT {e}")
    from .polymarket import PolymarketClient

    print(f"Polymarket: {len(PolymarketClient(cfg).search('Fed rate'))} liquide markten voor 'Fed rate'")
    return 0 if ok else 1


def cmd_simulate(args, cfg, bench):
    from .fakes import FakeLLM, FakeMarket, FakeNews, FakePoly, FakeWorld

    start = date.fromisoformat(args.start)
    world = FakeWorld(start)
    store = MemoryStore()
    repo = Repo(store)
    market = FakeMarket(world)
    llm = FakeLLM()
    for k in range(args.days):
        day = start + timedelta(days=k)
        for a in repo.approvals(["pending"]):  # simuleer: een mens keurt goed
            repo.set_approval(a["id"], {"status": "approved", "decided_by": "demo@voorbeeld.nl",
                                        "decided_at": datetime.combine(day, datetime.min.time(),
                                                                       tzinfo=timezone.utc).isoformat()})
        ctx = Context(cfg=cfg, bench=bench, repo=repo, news=FakeNews(world), poly=FakePoly(), market=market,
                      llm=llm, dry_run=True)
        res = DailyRun(ctx, day).run()
        snaps = store.select("portfolio_snapshots", [("day", "eq", day)])
        if not snaps:
            print(f"{day}  {res['status']}: {res['errors'][-1]['error'] if res['errors'] else ''}")
            print(res["errors"][-1].get("trace", ""))
            return 1
        snap = snaps[0]
        active = ", ".join(f"{t['title']} {t['weight'] * 100:.0f}%" for t in snap["topic_weights"]) or "-"
        print(f"{day}  {res['status']:<7} cash {snap['cash'] * 100:3.0f}%  {active}")
        for e in res["events"]:
            if e["kind"] in ("promotie", "uitstap", "shock_mutatie", "rebalance", "promotie_geblokkeerd",
                             "shock_goedkeuring_gevraagd", "nieuw_topic"):
                print(f"            - {e['kind']}: {e.get('topic', '')} {e.get('reden', '')}")
        for err in res["errors"]:
            print(f"            ! {err['where']}: {err['error']}")
    if args.out:
        tables = ["topics", "topic_daily", "approvals", "baskets", "basket_members", "portfolio_snapshots",
                  "runs", "api_usage", "topic_versions"]
        data = {t: store.select(t) for t in tables}
        data["app_users"] = [{"email": "demo@voorbeeld.nl", "display_name": "Demo", "role": "approver"}]
        shock = next((a for a in data["approvals"] if a["kind"] == "shock"), None)
        if shock:  # laat in de demo ook zien hoe een open verzoek eruitziet
            data["approvals"].append({**shock, "id": "demo-open", "status": "pending", "decided_by": None,
                                      "decided_at": None, "note": None,
                                      "expires_at": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat()})
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        print(f"Demo-data geschreven naar {args.out}")
    return 0


def cmd_add_user(args, cfg, bench):
    import httpx

    url, key = env("SUPABASE_URL").rstrip("/"), env("SUPABASE_SECRET_KEY")
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    email = args.email.strip().lower()
    password = args.password or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    r = httpx.post(f"{url}/auth/v1/admin/users", headers=headers, timeout=30,
                   json={"email": email, "password": password, "email_confirm": True})
    if r.status_code >= 400 and "already" not in r.text.lower():
        sys.exit(f"Aanmaken mislukt: {r.status_code} {r.text}")
    repo = supabase_repo()
    repo.s.upsert("app_users", [{"email": email, "display_name": args.name, "role": args.role}], "email")
    print(f"Toegang gegeven aan {email} ({args.role}). Tijdelijk wachtwoord: {password}")
    return 0


def main(argv=None):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--day")
    r.add_argument("--skip-if-done", action="store_true", help="niets doen als vandaag al een geslaagde run is")
    sub.add_parser("check")
    s = sub.add_parser("simulate")
    s.add_argument("--days", type=int, default=25)
    s.add_argument("--start", default="2026-10-01")
    s.add_argument("--out", default=str(ROOT / "dashboard" / "demo-data.json"))
    u = sub.add_parser("add-user")
    u.add_argument("--email", required=True)
    u.add_argument("--name")
    u.add_argument("--role", default="approver", choices=["viewer", "approver"])
    u.add_argument("--password")
    args = p.parse_args(argv)
    cfg, bench = load_settings(), load_benchmarks()
    handler = {"run": cmd_run, "check": cmd_check, "simulate": cmd_simulate, "add-user": cmd_add_user}[args.cmd]
    return handler(args, cfg, bench)


if __name__ == "__main__":
    sys.exit(main())
