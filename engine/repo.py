"""Domeinlaag bovenop de tabel-API."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

TRACKED = ["radar", "on_deck", "active"]

TOPIC_COLUMNS = [
    "id", "slug", "title", "status", "regime", "version", "main_macro_driver", "keywords", "benchmarks",
    "horizon_class", "polymarket_slugs", "source_event_uris", "mmp", "high_impact", "r_score", "streak_in",
    "gate_fail_streak", "low_i_streak", "protected_until", "first_seen", "promoted_at", "demoted_at",
    "retired_at", "merged_into", "last_driver_change", "meta", "updated_at",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repo:
    def __init__(self, store):
        self.s = store

    # Topics ---------------------------------------------------------------------
    def tracked_topics(self) -> list[dict]:
        return self.s.select("topics", [("status", "in", TRACKED)], order="created_at.asc")

    def all_slugs(self) -> set[str]:
        return {r["slug"] for r in self.s.select("topics", columns="slug")}

    def create_topic(self, t: dict) -> dict:
        row = {k: v for k, v in t.items() if k in TOPIC_COLUMNS and k != "id"}
        created = self.s.insert("topics", [row])[0]
        self.add_version(created, "aangemaakt")
        return created

    def save_topic(self, t: dict) -> None:
        row = {k: t.get(k) for k in TOPIC_COLUMNS}
        row["updated_at"] = now_iso()
        self.s.upsert("topics", [row], "id")

    def add_version(self, t: dict, reason: Optional[str]) -> None:
        """Legt de huidige versie vast. reason=None werkt alleen velden bij en laat de reden staan."""
        row = {
            "topic_id": t["id"], "version": t.get("version", 1), "title": t["title"],
            "main_macro_driver": t["main_macro_driver"], "keywords": t.get("keywords") or [],
            "benchmarks": t.get("benchmarks") or [], "horizon_class": t.get("horizon_class"),
        }
        if reason is not None:
            row["reason"] = reason
        self.s.upsert("topic_versions", [row], "topic_id,version")

    # Dagwaarden -------------------------------------------------------------------
    def history(self, topic_ids: list[str], since: date) -> list[dict]:
        if not topic_ids:
            return []
        return self.s.select("topic_daily", [("topic_id", "in", topic_ids), ("day", "gte", since)],
                             order="day.asc,topic_id.asc")

    def r_population(self, since: date) -> list[float]:
        rows = self.s.select("topic_daily", [("day", "gte", since)], columns="r_score",
                             order="day.asc,topic_id.asc")
        return [r["r_score"] for r in rows if r.get("r_score") is not None]

    def save_daily(self, rows: list[dict]) -> None:
        self.s.upsert("topic_daily", rows, "topic_id,day")

    # Goedkeuringen ----------------------------------------------------------------
    def approvals(self, statuses: list[str]) -> list[dict]:
        return self.s.select("approvals", [("status", "in", statuses)], order="created_at.asc")

    def approvals_for_topic(self, topic_id: str) -> list[dict]:
        return self.s.select("approvals", [("topic_id", "eq", topic_id)])

    def create_approval(self, row: dict) -> dict:
        return self.s.insert("approvals", [{"status": "pending", "payload": {}, **row}])[0]

    def set_approval(self, approval_id: str, values: dict) -> None:
        self.s.update("approvals", [("id", "eq", approval_id)], values)

    # Mandjes ----------------------------------------------------------------------
    def active_baskets(self) -> dict[str, dict]:
        baskets = self.s.select("baskets", [("status", "eq", "active")])
        if not baskets:
            return {}
        members = self.s.select("basket_members", [("basket_id", "in", [b["id"] for b in baskets])])
        out = {}
        for b in baskets:
            b["members"] = [m for m in members if m["basket_id"] == b["id"]]
            out[b["topic_id"]] = b
        return out

    def save_basket(self, topic: dict, reason: str, members: list[dict], cash: float, notes: dict) -> dict:
        self.s.update("baskets", [("topic_id", "eq", topic["id"]), ("status", "eq", "active")],
                      {"status": "archived"})
        b = self.s.insert("baskets", [{
            "topic_id": topic["id"], "topic_version": topic.get("version", 1), "reason": reason,
            "status": "active", "cash_share": cash, "notes": notes,
        }])[0]
        rows = [{**m, "basket_id": b["id"]} for m in members]
        self.s.upsert("basket_members", rows, "basket_id,symbol")
        b["members"] = rows
        return b

    def set_basket_status(self, topic_id: str, status: str) -> None:
        self.s.update("baskets", [("topic_id", "eq", topic_id), ("status", "eq", "active")], {"status": status})

    def unclosed_trades(self) -> list[dict]:
        """Mandjes die beëindigd zijn (uitstap of vervangen) maar nog geen uitstapkoers hebben."""
        rows = [b for b in self.s.select("baskets", [("status", "in", ["exit", "archived"])])
                if b.get("entry_day") and not b.get("exit_day")]
        if not rows:
            return []
        members = self.s.select("basket_members", [("basket_id", "in", [b["id"] for b in rows])])
        for b in rows:
            b["members"] = [m for m in members if m["basket_id"] == b["id"]]
        return rows

    def update_basket(self, basket_id: str, values: dict) -> None:
        self.s.update("baskets", [("id", "eq", basket_id)], values)

    def update_member(self, basket_id: str, symbol: str, values: dict) -> None:
        self.s.update("basket_members", [("basket_id", "eq", basket_id), ("symbol", "eq", symbol)], values)

    # Momentopnames ----------------------------------------------------------------
    def last_snapshot(self, before: date) -> Optional[dict]:
        rows = self.s.select("portfolio_snapshots", [("day", "lt", before)], order="day.desc", limit=1)
        return rows[0] if rows else None

    def save_snapshot(self, row: dict) -> None:
        self.s.upsert("portfolio_snapshots", [row], "day")

    # Koersen ----------------------------------------------------------------------
    def save_prices(self, rows: list[dict]) -> None:
        self.s.upsert("price_bars", rows, "symbol,day")

    def load_prices(self, symbols: list[str], since: date) -> list[dict]:
        if not symbols:
            return []
        out = []
        for i in range(0, len(symbols), 40):
            out += self.s.select("price_bars", [("symbol", "in", symbols[i:i + 40]), ("day", "gte", since)],
                                 order="day.asc,symbol.asc")
        return out

    # Verbruik en runs ---------------------------------------------------------------
    def usage(self, service: str, since: date, until: Optional[date] = None) -> float:
        f = [("service", "eq", service), ("day", "gte", since)]
        if until:
            f.append(("day", "lte", until))
        return sum(r["units"] for r in self.s.select("api_usage", f))

    def add_usage(self, day: date, service: str, units: float) -> None:
        rows = self.s.select("api_usage", [("day", "eq", day), ("service", "eq", service)])
        current = rows[0]["units"] if rows else 0.0
        self.s.upsert("api_usage", [{"day": day, "service": service, "units": current + units}], "day,service")

    def start_run(self, day: date, dry_run: bool) -> dict:
        return self.s.insert("runs", [{"run_day": day, "status": "running", "dry_run": dry_run}])[0]

    def finish_run(self, run_id: str, values: dict) -> None:
        self.s.update("runs", [("id", "eq", run_id)], {**values, "finished_at": now_iso()})
