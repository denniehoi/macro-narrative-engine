"""LLM-wrapper tegen een nagebootste Anthropic-API (geen sleutel of netwerk nodig)."""
import json

import httpx2 as httpx
import pytest

from engine.llm import LLM
from engine.settings import load_settings

CFG = load_settings()


def _msg(content, stop, usage=None):
    return {"id": "msg_x", "type": "message", "role": "assistant", "model": "m", "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": usage or {"input_tokens": 100, "output_tokens": 20}}


def _llm(responses, seen):
    import anthropic

    it = iter(responses)

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=next(it))

    llm = LLM.__new__(LLM)
    llm.cfg = CFG["models"]
    llm.usage = {"input": 0, "output": 0, "web_searches": 0}
    llm.client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                                     max_retries=0)
    return llm


def test_structured_forces_tool_and_returns_input():
    seen = []
    llm = _llm([_msg([{"type": "tool_use", "id": "t1", "name": "submit_alignment",
                       "input": {"alignment": 0.7}}], "tool_use")], seen)
    out = llm.structured("lite", "sys", {"a": 1}, "submit_alignment", {"type": "object"})
    assert out == {"alignment": 0.7}
    body = seen[0]
    assert body["model"] == CFG["models"]["lite"]
    assert "temperature" not in body
    assert body["tool_choice"] == {"type": "tool", "name": "submit_alignment"}
    assert llm.usage["input"] == 100


def test_research_handles_pause_turn_then_forces_submit():
    seen = []
    search_blocks = [
        {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "grid capex"}},
        {"type": "web_search_tool_result", "tool_use_id": "srv_1",
         "content": [{"type": "web_search_result", "url": "https://example.com", "title": "x",
                      "encrypted_content": "abc", "page_age": None}]},
    ]
    responses = [
        _msg(search_blocks, "pause_turn",
             {"input_tokens": 50, "output_tokens": 10, "server_tool_use": {"web_search_requests": 1}}),
        _msg([{"type": "text", "text": "Klaar met zoeken.", "citations": None}], "end_turn"),
        _msg([{"type": "tool_use", "id": "t2", "name": "submit_candidates", "input": {"candidates": []}}], "tool_use"),
    ]
    llm = _llm(responses, seen)
    out = llm.research("stock_search", "sys", {"topic": "x"}, "submit_candidates", {"type": "object"})
    assert out == {"candidates": []}
    assert seen[0]["tools"][0]["type"] == CFG["models"]["web_search_tool"]
    assert seen[0]["tool_choice"] == {"type": "auto"}
    assert seen[1]["messages"][-1]["role"] == "assistant"          # pause_turn: ongewijzigd terugsturen
    assert seen[2]["tool_choice"] == {"type": "tool", "name": "submit_candidates"}
    assert llm.usage["web_searches"] == 1
