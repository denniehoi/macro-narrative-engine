"""newsAPI.ai (Event Registry) met harde token-limieten per dag en per maand."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable

import httpx

log = logging.getLogger(__name__)

COST_ARTICLES = 1      # recente artikel-zoekactie
COST_EVENTS = 5        # recente event-zoekactie
COST_TIME_AGGR = 5     # simple aggregate


def _lang(v, default=""):
    """Event-velden zijn objecten per taal: pak Engels, anders de eerste."""
    if isinstance(v, dict):
        return v.get("eng") or next(iter(v.values()), default)
    return v or default


class TokenBudgetExceeded(RuntimeError):
    pass


class NewsApiError(RuntimeError):
    pass


class NewsApiClient:
    def __init__(self, api_key: str, cfg: dict, used_today: Callable[[], float],
                 used_month: Callable[[], float], record: Callable[[float], None]):
        self.key = api_key
        self.cfg = cfg["newsapi"]
        self.used_today, self.used_month, self.record = used_today, used_month, record
        self.http = httpx.Client(base_url=self.cfg["base_url"], timeout=60)
        self.spent = 0.0

    def _post(self, path: str, body: dict, cost: float) -> dict:
        if self.used_today() + cost > self.cfg["daily_token_limit"]:
            raise TokenBudgetExceeded(f"daglimiet {self.cfg['daily_token_limit']} tokens bereikt")
        if self.used_month() + cost > self.cfg["monthly_token_limit"]:
            raise TokenBudgetExceeded(f"maandlimiet {self.cfg['monthly_token_limit']} tokens bereikt")
        r = self.http.post(path, json={**body, "apiKey": self.key})
        self.record(cost)
        self.spent += cost
        if r.status_code >= 400:
            raise NewsApiError(f"{path}: {r.status_code} {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise NewsApiError(f"{path}: {data['error']}")
        return data

    def _base(self, day: date) -> dict:
        return {"lang": self.cfg["lang"], "dateStart": day.isoformat(), "dateEnd": day.isoformat(),
                "forceMaxDataTimeWindow": 31}

    def topic_articles(self, keywords: list[str], day: date) -> dict:
        """Volume (totalResults) en een steekproef van artikelen voor de alignment-check. 1 token."""
        body = {
            **self._base(day),
            "keyword": keywords, "keywordOper": "or", "keywordLoc": "body,title",
            "isDuplicateFilter": "skipDuplicates", "dataType": ["news"],
            "articlesSortBy": "rel", "articlesCount": self.cfg["articles_per_topic"],
            "includeArticleSentiment": True, "resultType": "articles",
        }
        data = self._post("/article/getArticles", body, COST_ARTICLES)
        arts = data.get("articles", {})
        results = [{
            "title": a.get("title"), "body": (a.get("body") or "")[:400], "url": a.get("url"),
            "date": a.get("date"), "sentiment": a.get("sentiment"),
            "source": (a.get("source") or {}).get("title"),
        } for a in arts.get("results", [])]
        return {"total": int(arts.get("totalResults", len(results))), "articles": results}

    def topic_counts(self, keywords: list[str], end: date, days: int) -> dict[str, int]:
        """Dagelijkse aantallen voor de basislijn van een nieuw topic. 5 tokens, eenmalig."""
        start = end - timedelta(days=days - 1)
        body = {
            "lang": self.cfg["lang"], "dateStart": start.isoformat(), "dateEnd": end.isoformat(),
            "forceMaxDataTimeWindow": 31, "keyword": keywords, "keywordOper": "or",
            "keywordLoc": "body,title", "isDuplicateFilter": "skipDuplicates", "resultType": "timeAggr",
        }
        data = self._post("/article/getArticles", body, COST_TIME_AGGR)
        agg = data.get("timeAggr", data)
        results = agg.get("results", agg) if isinstance(agg, dict) else agg
        out = {}
        for row in results or []:
            if isinstance(row, dict) and "date" in row:
                out[str(row["date"])[:10]] = int(row.get("count", 0))
        return out

    def discovery_events(self, day: date) -> list[dict]:
        """Grootste events van de dag in business/politiek/tech/wetenschap. 5 tokens."""
        d = self.cfg["discovery"]
        body = {
            **self._base(day),
            "categoryUri": d["category_uris"], "eventsSortBy": "size",
            "eventsCount": d["events_count"], "minArticlesInEvent": d["min_articles_in_event"],
            "includeEventSummary": True, "includeEventConcepts": True, "resultType": "events",
        }
        data = self._post("/event/getEvents", body, COST_EVENTS)
        out = []
        for e in data.get("events", {}).get("results", []):
            out.append({
                "uri": e.get("uri"),
                "title": _lang(e.get("title")),
                "summary": str(_lang(e.get("summary")))[:600],
                "articles": e.get("totalArticleCount"),
                "date": e.get("eventDate"),
                "sentiment": e.get("sentiment"),
                "concepts": [_lang(c.get("label"), c.get("uri")) for c in (e.get("concepts") or [])[:6]],
                "categories": [_lang(c.get("label"), c.get("uri")) for c in (e.get("categories") or [])[:3]],
            })
        return out
