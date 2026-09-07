"""Loop tests with a fake client. No API key, no network."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dayflow import loop


def _resp(stop, content, i=100, o=50):
    return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=i, output_tokens=o))


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


def test_single_tool_round_trip():
    client = FakeClient([
        _resp("tool_use", [_tool("read_tasks", {})]),
        _resp("end_turn", [_text("brief")]),
    ])
    r = loop.run("sys", "go", client=client)
    assert r.text == "brief"
    assert r.steps == 2
    assert r.tool_calls[0]["tool"] == "read_tasks"
    # second call carries the tool_result back in a user turn with the matching id
    second = client.calls[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["tool_use_id"] == "tu_1"


def test_max_steps_guard():
    client = FakeClient([_resp("tool_use", [_tool("read_tasks", {})]) for _ in range(5)])
    with pytest.raises(loop.LoopError, match="no end_turn after 3"):
        loop.run("sys", "go", client=client, max_steps=3)


def test_token_budget_guard():
    client = FakeClient([_resp("tool_use", [_tool("read_tasks", {})], i=5000, o=1000)])
    with pytest.raises(loop.LoopError, match="token budget"):
        loop.run("sys", "go", client=client, token_budget=1000)


def test_max_tokens_is_a_failure_not_a_result():
    client = FakeClient([_resp("max_tokens", [_text("truncated...")])])
    with pytest.raises(loop.LoopError, match="max_tokens"):
        loop.run("sys", "go", client=client)


def test_unknown_tool_returns_error_to_model():
    client = FakeClient([
        _resp("tool_use", [_tool("nope", {})]),
        _resp("end_turn", [_text("ok")]),
    ])
    loop.run("sys", "go", client=client)
    result_block = client.calls[1]["messages"][-1]["content"][0]
    assert "unknown tool" in result_block["content"]


def test_tool_exception_becomes_error_result(monkeypatch):
    from dayflow import tools

    def boom(**_):
        raise ValueError("inbox unavailable")

    monkeypatch.setitem(tools._HANDLERS, "read_inbox", boom)
    client = FakeClient([
        _resp("tool_use", [_tool("read_inbox", {})]),
        _resp("end_turn", [_text("ok")]),
    ])
    loop.run("sys", "go", client=client)
    result_block = client.calls[1]["messages"][-1]["content"][0]
    assert "inbox unavailable" in result_block["content"]
