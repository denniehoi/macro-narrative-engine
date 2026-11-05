"""LLM-wrapper tegen een nagebootste Anthropic-API (geen sleutel of netwerk nodig)."""
import json

import httpx2 as httpx
import pytest

from engine import prompts
from engine.llm import LLM, LLMError, strict_schema
from engine.settings import load_settings

CFG = load_settings()


def _msg(content, stop, usage=None):
    return {"id": "msg_x", "type": "message", "role": "assistant", "model": "m", "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": usage or {"input_tokens": 100, "output_tokens": 20}}


def _text(obj):
    return {"type": "text", "text": json.dumps(obj), "citations": None}


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


def _walk(s):
    if isinstance(s, dict):
        yield s
        for v in s.values():
            yield from _walk(v)
    elif isinstance(s, list):
        for v in s:
            yield from _walk(v)


@pytest.mark.parametrize("schema", [prompts.DISCOVERY_SCHEMA, prompts.ALIGNMENT_SCHEMA,
                                    prompts.reasoning_schema(["XLU", "BZ=F"]), prompts.STOCK_CANDIDATES_SCHEMA,
                                    prompts.STOCK_SELECT_SCHEMA])
def test_schemas_are_valid_for_structured_outputs(schema):
    s = strict_schema(schema)
    for node in _walk(s):
        t = node.get("type")
        if t == "object" or (isinstance(t, list) and "object" in t):
            assert node.get("additionalProperties") is False
        for bad in ("minimum", "maximum", "maxItems", "minLength", "maxLength"):
            assert bad not in node or isinstance(node.get("properties"), dict) and bad in node["properties"]
        if "minItems" in node:
            assert node["minItems"] in (0, 1)


def test_structured_uses_json_schema_output_and_parses_text():
    seen = []
    llm = _llm([_msg([_text({"alignment": 0.7})], "end_turn")], seen)
    out = llm.structured("reasoning", "sys", {"a": 1}, "submit_alignment", prompts.ALIGNMENT_SCHEMA)
    assert out == {"alignment": 0.7}
    body = seen[0]
    assert body["model"] == CFG["models"]["reasoning"]
    assert "tool_choice" not in body and "tools" not in body and "temperature" not in body
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert llm.usage["input"] == 100


def test_structured_raises_on_refusal():
    llm = _llm([_msg([{"type": "text", "text": "nee", "citations": None}], "refusal")], [])
    with pytest.raises(LLMError):
        llm.structured("lite", "sys", "x", "t", {"type": "object", "properties": {}})


def test_research_searches_then_structures():
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
        _msg([{"type": "text", "text": "PWR: 40% omzet uit netwerken.", "citations": [
            {"type": "web_search_result_location", "url": "https://example.com/pwr", "title": "PWR 10-K",
             "encrypted_index": "x", "cited_text": "40%"}]}], "end_turn"),
        _msg([_text({"candidates": []})], "end_turn"),
    ]
    llm = _llm(responses, seen)
    out = llm.research("stock_search", "sys", {"topic": "x"}, "submit_candidates", prompts.STOCK_CANDIDATES_SCHEMA)
    assert out == {"candidates": []}
    assert seen[0]["tools"][0]["type"] == CFG["models"]["web_search_tool"] and "output_config" not in seen[0]
    assert seen[1]["messages"][-1]["role"] == "assistant"          # pause_turn: ongewijzigd terugsturen
    assert "output_config" in seen[2] and "tools" not in seen[2]
    assert "https://example.com/pwr" in seen[2]["messages"][0]["content"]
    assert llm.usage["web_searches"] == 1


def test_structured_retries_once_with_more_room_after_cutoff():
    seen = []
    llm = _llm([_msg([{"type": "text", "text": '{"candid', "citations": None}], "max_tokens"),
                _msg([_text({"candidates": []})], "end_turn")], seen)
    out = llm.structured("lite", "sys", "x", "submit_discovery", prompts.DISCOVERY_SCHEMA, max_tokens=8000)
    assert out == {"candidates": []}
    assert seen[0]["max_tokens"] == 8000 and seen[1]["max_tokens"] == 16000
