"""Opslag: Supabase (PostgREST) of in-memory (tests en dry-run) met dezelfde tabel-API.

filters: lijst van (kolom, operator, waarde) met operator in eq, neq, in, gte, lte, gt, lt.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import httpx


def _json_default(o: Any):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"Niet serialiseerbaar: {type(o)}")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)


class SupabaseStore:
    def __init__(self, url: str, key: str, timeout: float = 30.0):
        headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy service_role JWT
            headers["Authorization"] = f"Bearer {key}"
        self.client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", headers=headers, timeout=timeout)

    @staticmethod
    def _params(filters: Optional[Iterable] = None, order: Optional[str] = None,
                limit: Optional[int] = None, columns: str = "*") -> list[tuple[str, str]]:
        params = [("select", columns)]
        for col, op, val in filters or []:
            if op == "in":
                vals = ",".join(f'"{v}"' for v in val)
                params.append((col, f"in.({vals})"))
            else:
                v = val.isoformat() if isinstance(val, (date, datetime)) else val
                if isinstance(v, bool):
                    v = str(v).lower()
                params.append((col, f"{op}.{v}"))
        if order:
            params.append(("order", order))
        if limit:
            params.append(("limit", str(limit)))
        return params

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {r.request.method} {r.request.url.path}: {r.status_code} {r.text[:500]}")
        return r

    PAGE = 1000  # Supabase geeft standaard maximaal 1000 rijen per verzoek

    def select(self, table: str, filters=None, order=None, limit=None, columns="*") -> list[dict]:
        if filters and any(op == "in" and not val for _, op, val in filters):
            return []
        if limit:
            r = self.client.get(f"/{table}", params=self._params(filters, order, limit, columns))
            return self._check(r).json()
        out: list[dict] = []
        while True:
            params = self._params(filters, order, self.PAGE, columns) + [("offset", str(len(out)))]
            page = self._check(self.client.get(f"/{table}", params=params)).json()
            out += page
            if len(page) < self.PAGE:
                return out

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not rows:
            return
        # PostgREST eist gelijke sleutels per batch: groepeer op sleutelset.
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            groups.setdefault(tuple(sorted(row)), []).append(row)
        for batch in groups.values():
            for i in range(0, len(batch), 500):
                r = self.client.post(
                    f"/{table}", params={"on_conflict": on_conflict}, content=_dumps(batch[i:i + 500]),
                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
                self._check(r)

    def insert(self, table: str, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        r = self.client.post(f"/{table}", content=_dumps(rows), headers={"Prefer": "return=representation"})
        return self._check(r).json()

    def update(self, table: str, filters, values: dict) -> None:
        r = self.client.patch(f"/{table}", params=self._params(filters)[1:], content=_dumps(values),
                              headers={"Prefer": "return=minimal"})
        self._check(r)


class MemoryStore:
    """Zelfde interface, voor tests en --dry-run."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def _rows(self, table):
        return self.tables.setdefault(table, [])

    @staticmethod
    def _norm(v):
        return v.isoformat() if isinstance(v, (date, datetime)) else v

    @classmethod
    def _match(cls, row, filters):
        for col, op, val in filters or []:
            v = cls._norm(row.get(col))
            val = [cls._norm(x) for x in val] if op == "in" else cls._norm(val)
            if op == "eq" and v != val: return False
            if op == "neq" and v == val: return False
            if op == "in" and v not in val: return False
            if op in ("gte", "lte", "gt", "lt"):
                if v is None: return False
                if op == "gte" and not v >= val: return False
                if op == "lte" and not v <= val: return False
                if op == "gt" and not v > val: return False
                if op == "lt" and not v < val: return False
        return True

    def select(self, table, filters=None, order=None, limit=None, columns="*"):
        rows = [json.loads(_dumps(r)) for r in self._rows(table) if self._match(r, filters)]
        if order:
            for part in reversed(order.split(",")):
                col, _, direction = part.partition(".")
                rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=(direction == "desc"))
        return rows[:limit] if limit else rows

    def upsert(self, table, rows, on_conflict):
        keys = on_conflict.split(",")
        existing = self._rows(table)
        for row in rows:
            row = json.loads(_dumps(row))
            for r in existing:
                if all(r.get(k) == row.get(k) for k in keys):
                    r.update(row)
                    break
            else:
                existing.append(self._defaults(table, row))

    def insert(self, table, rows):
        out = []
        for row in rows:
            row = self._defaults(table, json.loads(_dumps(row)))
            self._rows(table).append(row)
            out.append(dict(row))
        return out

    def update(self, table, filters, values):
        values = json.loads(_dumps(values))
        for r in self._rows(table):
            if self._match(r, filters):
                r.update(values)

    @staticmethod
    def _defaults(table, row):
        if table in {"topics", "approvals", "baskets", "runs"} and not row.get("id"):
            row["id"] = str(uuid.uuid4())
        row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        return row
