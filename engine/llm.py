"""Anthropic-API met gestructureerde uitvoer (output_config.format = json_schema).

- Werkt op elk model. Afgedwongen tool-aanroepen (tool_choice "tool") worden door Sonnet 5.5 niet
  ondersteund, dus die gebruiken we niet.
- De SDK (2.x) kent geen temperature meer. Consistentie komt van schema-gebonden uitvoer en discrete
  klassen in plaats van vrije getallen.
- JSON-uitvoer gaat niet samen met citaties van web search. Daarom werkt research() in twee stappen:
  eerst zoeken (vrije tekst met bronnen), daarna dezelfde inhoud omzetten naar het schema.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
               "maxLength", "maxItems", "uniqueItems"}


class LLMError(RuntimeError):
    pass


def strict_schema(s):
    """Maak een JSON-schema geschikt voor structured outputs: additionalProperties false op elk object,
    niet-ondersteunde beperkingen verhuizen naar de beschrijving (de code begrenst daarna zelf)."""
    if isinstance(s, list):
        return [strict_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    out, notes = {}, []
    for k, v in s.items():
        if k in UNSUPPORTED or (k == "minItems" and v not in (0, 1)):
            notes.append(f"{k} {v}")
        elif k == "properties":
            out[k] = {name: strict_schema(sub) for name, sub in v.items()}
        elif k in ("items", "anyOf", "allOf", "oneOf"):
            out[k] = strict_schema(v)
        else:
            out[k] = v
    t = out.get("type")
    if t == "object" or (isinstance(t, list) and "object" in t):
        out["additionalProperties"] = False
    if notes:
        out["description"] = (out.get("description", "") + " (" + ", ".join(notes) + ")").strip()
    return out


class LLM:
    def __init__(self, api_key: str, cfg: dict):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self.cfg = cfg["models"]
        self.usage = {"input": 0, "output": 0, "web_searches": 0}

    def _track(self, resp) -> None:
        u = getattr(resp, "usage", None)
        if not u:
            return
        self.usage["input"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output"] += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            self.usage["web_searches"] += getattr(stu, "web_search_requests", 0) or 0

    @staticmethod
    def _text(resp) -> str:
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")

    def structured(self, model_key: str, system: str, payload: dict | str, tool_name: str, schema: dict,
                   max_tokens: Optional[int] = None) -> dict:
        user = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        limit = max_tokens or self.cfg["max_tokens"]
        for attempt in range(2):
            resp = self.client.messages.create(
                model=self.cfg[model_key], max_tokens=limit,
                system=system, messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": strict_schema(schema)}},
            )
            self._track(resp)
            if resp.stop_reason != "max_tokens" or attempt == 1:
                break
            limit = min(limit * 2, 32000)  # antwoord afgekapt: één keer opnieuw met meer ruimte
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"{tool_name}: gestopt ({resp.stop_reason})")
        try:
            return json.loads(self._text(resp))
        except ValueError as e:
            raise LLMError(f"{tool_name}: geen geldige JSON") from e

    def research(self, model_key: str, system: str, payload: dict | str, tool_name: str, schema: dict,
                 max_rounds: int = 6) -> dict:
        """Stap 1: zoeken met web search (vrije tekst met bronnen). Stap 2: omzetten naar het schema."""
        user = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        tools = [{"type": self.cfg["web_search_tool"], "name": "web_search",
                  "max_uses": self.cfg["web_search_max_uses"]}]
        messages = [{"role": "user", "content": user + "\n\nGeef je resultaat als overzichtelijke lijst, "
                                                         "met per kandidaat de bron-URL."}]
        notes: list[str] = []
        for _ in range(max_rounds):
            resp = self.client.messages.create(
                model=self.cfg[model_key], max_tokens=max(8000, self.cfg["max_tokens"]),
                system=system, messages=messages, tools=tools,
            )
            self._track(resp)
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    notes.append(b.text)
                    for c in getattr(b, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url:
                            notes.append(f"[bron: {url}]")
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        findings = "\n".join(notes).strip()
        if not findings:
            raise LLMError(f"{tool_name}: zoekstap leverde niets op")
        return self.structured(
            model_key, "Zet de onderzoeksresultaten om naar het gevraagde schema. Neem alleen kandidaten op die "
                       "in de tekst staan en gebruik de genoemde bron-URL's. Verzin niets.",
            {"opdracht": payload, "onderzoeksresultaten": findings[:60000]}, tool_name, schema,
            max_tokens=max(8000, self.cfg["max_tokens"]))
