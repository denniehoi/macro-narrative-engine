"""Anthropic-API: gestructureerde uitvoer via geforceerde tool-calls.

De huidige SDK (2.x) en modellen kennen geen temperature-parameter meer. Consistentie komt daarom van
geforceerde, schema-gebonden uitvoer en discrete klassen (horizon, tier volgt uit omzetaandeel) in plaats
van vrije getallen.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


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
    def _tool(name: str, schema: dict) -> dict:
        return {"name": name, "description": f"Lever het resultaat ({name}).", "input_schema": schema}

    @staticmethod
    def _find_tool_input(resp, name: str) -> Optional[dict]:
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == name:
                return block.input
        return None

    def structured(self, model_key: str, system: str, payload: dict | str, tool_name: str, schema: dict,
                   max_tokens: Optional[int] = None) -> dict:
        user = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        resp = self.client.messages.create(
            model=self.cfg[model_key], max_tokens=max_tokens or self.cfg["max_tokens"],
            system=system, messages=[{"role": "user", "content": user}],
            tools=[self._tool(tool_name, schema)], tool_choice={"type": "tool", "name": tool_name},
        )
        self._track(resp)
        out = self._find_tool_input(resp, tool_name)
        if out is None:
            raise LLMError(f"{tool_name}: geen tool-uitvoer")
        return out

    def research(self, model_key: str, system: str, payload: dict | str, tool_name: str, schema: dict,
                 max_rounds: int = 6) -> dict:
        """Web search + afsluitende tool-call. Handelt pause_turn af en dwingt de tool af als het model stopt."""
        user = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        tools = [
            {"type": self.cfg["web_search_tool"], "name": "web_search", "max_uses": self.cfg["web_search_max_uses"]},
            self._tool(tool_name, schema),
        ]
        messages = [{"role": "user", "content": user}]
        tool_choice = {"type": "auto"}
        for _ in range(max_rounds):
            resp = self.client.messages.create(
                model=self.cfg[model_key], max_tokens=max(8000, self.cfg["max_tokens"]),
                system=system, messages=messages,
                tools=tools, tool_choice=tool_choice,
            )
            self._track(resp)
            out = self._find_tool_input(resp, tool_name)
            if out is not None:
                return out
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue
            messages.append({"role": "user", "content": f"Roep nu {tool_name} aan met je volledige resultaat."})
            tool_choice = {"type": "tool", "name": tool_name}
        raise LLMError(f"{tool_name}: geen resultaat na {max_rounds} rondes")
