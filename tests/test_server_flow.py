"""End-to-end tests for the request handler with a mocked brain.

These drive the real FastAPI app but replace the browser layer, so they
exercise the actual prompt building, reply parsing, retry policy, error
mapping and SSE framing - without touching a real web account.

The behaviour under test is the one that matters most for this project:
the proxy must never hand Hermes a result that no tool produced.
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from breaker import Breaker  # noqa: E402


@pytest.fixture()
def app_mod(monkeypatch):
    """Import server.py with the browser module stubbed out."""
    stub = types.ModuleType("browser")
    stub.BRAIN_PREAMBLE = "[BRAIN PROXY]\n"
    stub.BRAIN_PREAMBLE_PLAIN = "[BRAIN PROXY]\n"

    class BrainBrowser:
        def __init__(self, *a, **k):
            self.paused = False
            self.max_turns = 40
            self.reanchor_every = 10
            self.breaker = Breaker()

        def wants_rotation(self):
            return False

        async def ask(self, prompt, files=None, task_id=None):
            raise NotImplementedError

        async def browser_needs_relaunch(self):
            return False

    stub.BrainBrowser = BrainBrowser
    monkeypatch.setitem(sys.modules, "browser", stub)

    import server

    # call_helper spawns the real helper CLI (a 90s subprocess). The suite is
    # supposed to be deterministic and offline, so stub it out; the helper
    # fallback itself gets its own test below.
    async def _no_helper(messages, tools=None, kind="chat"):
        return None

    monkeypatch.setattr(server, "call_helper", _no_helper)
    # BROWSERS is a module global; without clearing it a breaker left open by
    # one test would bench the brain for the next.
    server.BROWSERS.clear()

    return server


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_cmd",
            "description": "Run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


def _patch_brain(monkeypatch, mod, replies):
    """Make brain_call return `replies` in order (last one repeats)."""
    calls = []

    async def fake_brain_call(name, prompt, max_wait=None, files=None,
                              task_id=None, client_key=None):
        calls.append(prompt)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r, name

    monkeypatch.setattr(mod, "brain_call", fake_brain_call)
    return calls


def _request_body(**over):
    body = {
        "model": "chatgpt",
        "messages": [{"role": "user", "content": "list the files please"}],
    }
    body.update(over)
    return body


class TestToolCallFidelity:
    def test_clean_tool_call_becomes_a_tool_call(self, app_mod, monkeypatch):
        _patch_brain(monkeypatch, app_mod, [
            '{"tool": "run_cmd", "args": {"command": "ls -la"}}'
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert out["choices"][0]["finish_reason"] == "tool_calls"
        tc = out["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "run_cmd"
        assert json.loads(tc["function"]["arguments"]) == {"command": "ls -la"}

    def test_mangled_markdown_still_becomes_a_tool_call(self, app_mod, monkeypatch):
        """This is the bug that made the brain look like it was hallucinating."""
        _patch_brain(monkeypatch, app_mod, [
            'Sure!\n```json\n{"tool": "run_cmd", "args": {"command": "ls -la"}}\n```\n'
            'Hope that helps!'
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert out["choices"][0]["finish_reason"] == "tool_calls"
        tc = out["choices"][0]["message"]["tool_calls"][0]
        assert json.loads(tc["function"]["arguments"]) == {"command": "ls -la"}

    def test_smart_quotes_still_become_a_tool_call(self, app_mod, monkeypatch):
        _patch_brain(monkeypatch, app_mod, [
            '{“tool”: “run_cmd”, “args”: {“command”: “ls -la”}}'
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert out["choices"][0]["finish_reason"] == "tool_calls"


class TestNoInventedResults:
    """The brain must never fake a tool result in prose."""

    def test_prose_claiming_a_result_is_not_returned_as_an_answer(
        self, app_mod, monkeypatch
    ):
        # The brain claims a command returned data - but never issued a tool call.
        _patch_brain(monkeypatch, app_mod, [
            "The command returned three files: a.txt, b.txt and c.txt."
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        # It must NOT come back as a normal completion.
        assert "choices" not in out, "prose was passed through as a finished answer"
        assert out["error"]["code"] == "unparseable_brain_reply"

    def test_prose_falls_back_to_retrying_the_brain(self, app_mod, monkeypatch):
        calls = _patch_brain(monkeypatch, app_mod, [
            "I ran it and got three files.",
            '{"tool": "run_cmd", "args": {"command": "ls"}}',
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert len(calls) == 2, "the brain should have been asked a second time"
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_unparseable_after_retry_is_a_hard_error(self, app_mod, monkeypatch):
        calls = _patch_brain(monkeypatch, app_mod, ["not json", "still not json"])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert len(calls) == 2
        assert "error" in out
        assert out["error"]["code"] == "unparseable_brain_reply"

    def test_hallucinated_tool_name_is_rejected(self, app_mod, monkeypatch):
        """A brain asking for a tool that does not exist must not be obeyed."""
        _patch_brain(monkeypatch, app_mod, [
            '{"tool": "delete_all_files", "args": {}}'
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert "error" in out
        assert out["error"]["code"] == "unparseable_brain_reply"

    def test_prose_is_fine_when_no_tools_are_offered(self, app_mod, monkeypatch):
        """Plain chat: prose is the answer, not a protocol violation."""
        _patch_brain(monkeypatch, app_mod, ["Hello! How can I help you today?"])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body()))))
        assert out["choices"][0]["finish_reason"] == "stop"
        assert "How can I help" in out["choices"][0]["message"]["content"]


class TestHelperFallback:
    def test_helper_supplies_the_tool_call_when_the_brain_writes_prose(
        self, app_mod, monkeypatch
    ):
        """The helper is the last resort before failing loudly."""
        _patch_brain(monkeypatch, app_mod, [
            "I ran it and got three files.",
            "I already told you the result.",
        ])

        async def _helper(messages, tools=None, kind="chat"):
            assert kind == "tool_json"
            return '{"tool": "run_cmd", "args": {"command": "ls"}}'

        monkeypatch.setattr(app_mod, "call_helper", _helper)

        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert out["choices"][0]["finish_reason"] == "tool_calls"
        tc = out["choices"][0]["message"]["tool_calls"][0]
        assert json.loads(tc["function"]["arguments"]) == {"command": "ls"}

    def test_helper_output_is_never_passed_through_unvalidated(
        self, app_mod, monkeypatch
    ):
        """A helper that answers in prose must not become a finished answer."""
        _patch_brain(monkeypatch, app_mod, ["not json", "still not json"])

        async def _helper(messages, tools=None, kind="chat"):
            return "sure, here is the answer you wanted"

        monkeypatch.setattr(app_mod, "call_helper", _helper)

        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert "choices" not in out
        assert out["error"]["code"] == "unparseable_brain_reply"


class TestToolRouting:
    """A brain that has its own workspace should not be handed every tool."""

    def test_a_brain_with_no_setting_is_offered_everything(self, app_mod):
        assert app_mod.advertised_tools("chatgpt", TOOLS) == TOOLS

    def test_an_empty_list_offers_nothing(self, app_mod):
        assert app_mod.advertised_tools("arena-agent", TOOLS) == []

    def test_only_the_listed_tools_are_offered(self, app_mod, monkeypatch):
        monkeypatch.setitem(app_mod.CFG["brains"]["arena-agent"],
                            "advertise_tools", ["run_cmd"])
        extra = {"type": "function", "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {}}}}
        kept = app_mod.advertised_tools("arena-agent", TOOLS + [extra])
        assert [t["function"]["name"] for t in kept] == ["run_cmd"]

    def test_no_tools_block_reaches_a_tool_less_brain(self, app_mod, monkeypatch):
        calls = _patch_brain(monkeypatch, app_mod, ["done"])
        asyncio.run(app_mod.chat_completions(
            _FakeReq(_request_body(model="arena-agent", tools=TOOLS))))
        assert "get_weather" not in calls[0]

    def test_prose_is_the_answer_when_no_tools_are_advertised(
        self, app_mod, monkeypatch
    ):
        """With nothing advertised there is no protocol to violate."""
        _patch_brain(monkeypatch, app_mod, ["I did the work in my workspace."])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(
                _request_body(model="arena-agent", tools=TOOLS)))))
        assert out["choices"][0]["finish_reason"] == "stop"
        assert "workspace" in out["choices"][0]["message"]["content"]

    def test_advertised_tools_are_still_validated(self, app_mod, monkeypatch):
        """A brain cannot smuggle in a tool that was not advertised."""
        monkeypatch.setitem(app_mod.CFG["brains"]["arena-agent"],
                            "advertise_tools", ["run_cmd"])
        _patch_brain(monkeypatch, app_mod, [
            '{"tool": "delete_all_files", "args": {}}'
        ])
        out = _body(asyncio.run(
            app_mod.chat_completions(_FakeReq(
                _request_body(model="arena-agent", tools=TOOLS)))))
        assert "error" in out


class TestStreaming:
    def test_streaming_tool_call_frames(self, app_mod, monkeypatch):
        _patch_brain(monkeypatch, app_mod, [
            '{"tool": "run_cmd", "args": {"command": "ls"}}'
        ])
        resp = asyncio.run(app_mod.chat_completions(
            _FakeReq(_request_body(tools=TOOLS, stream=True))))
        chunks = _drain(resp)
        assert chunks, "expected at least one SSE chunk"
        payloads = _sse_payloads(chunks)
        assert payloads[-1] == "[DONE]", "the stream must terminate with [DONE]"
        tool_chunks = [p for p in payloads
                       if isinstance(p, dict) and "choices" in p
                       and p["choices"][0]["delta"].get("tool_calls")]
        assert tool_chunks, "expected a tool_calls delta in the stream"
        args = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(args) == {"command": "ls"}

    def test_streaming_releases_the_concurrency_semaphore(self, app_mod, monkeypatch):
        """Regression: streaming used to leak a semaphore slot every call."""
        _patch_brain(monkeypatch, app_mod, ['{"final": "done"}'])
        mod = app_mod
        before = mod._ACTIVE._value
        for _ in range(mod.MAX_CONCURRENT + 2):
            resp = asyncio.run(mod.chat_completions(
                _FakeReq(_request_body(stream=True))))
            _drain(resp)
        assert mod._ACTIVE._value == before, \
            "streaming leaked a concurrency slot"

    def test_streaming_error_is_reported_in_the_stream(self, app_mod, monkeypatch):
        _patch_brain(monkeypatch, app_mod, ["not json at all"])
        resp = asyncio.run(app_mod.chat_completions(
            _FakeReq(_request_body(tools=TOOLS, stream=True))))
        chunks = _drain(resp)
        joined = "".join(chunks)
        assert "unparseable_brain_reply" in joined


class TestConcurrencyAccounting:
    """The concurrency slot must be released on every path."""

    def test_non_streaming_releases_the_slot(self, app_mod, monkeypatch):
        _patch_brain(monkeypatch, app_mod, ['{"final": "done"}'])
        mod = app_mod
        before = mod._ACTIVE._value
        for _ in range(mod.MAX_CONCURRENT + 2):
            out = _body(asyncio.run(
                mod.chat_completions(_FakeReq(_request_body()))))
            assert "choices" in out
        assert mod._ACTIVE._value == before

    def test_failed_request_releases_the_slot(self, app_mod, monkeypatch):
        _patch_brain(monkeypatch, app_mod, ["not json", "still not json"])
        mod = app_mod
        before = mod._ACTIVE._value
        out = _body(asyncio.run(
            mod.chat_completions(_FakeReq(_request_body(tools=TOOLS)))))
        assert out["error"]["code"] == "unparseable_brain_reply"
        assert mod._ACTIVE._value == before

    def test_brain_failure_releases_the_slot(self, app_mod, monkeypatch):
        async def boom(name, prompt, max_wait=None, files=None, task_id=None):
            raise RuntimeError("browser exploded")

        monkeypatch.setattr(app_mod, "brain_call", boom)
        mod = app_mod
        before = mod._ACTIVE._value
        out = _body(asyncio.run(mod.chat_completions(_FakeReq(_request_body()))))
        assert "error" in out
        assert mod._ACTIVE._value == before


class TestBreakerRecovery:
    """A dead browser must not leave a brain benched forever."""

    def test_an_open_circuit_recovers_when_the_browser_is_dead(
        self, app_mod, monkeypatch
    ):
        mod = app_mod
        b = asyncio.run(mod.get_browser("chatgpt"))
        b.breaker.fail("cdp_dead", 300)
        assert b.breaker.state == "open"

        async def dead():
            return True

        monkeypatch.setattr(b, "browser_needs_relaunch", dead)

        async def fake_ask(prompt, files=None, task_id=None, client_key=None):
            return "back"

        monkeypatch.setattr(b, "ask", fake_ask)

        out = _body(asyncio.run(
            mod.chat_completions(_FakeReq(_request_body()))))
        assert "choices" in out, out
        assert b.breaker.state == "closed"

    def test_an_open_circuit_still_blocks_when_the_browser_is_fine(
        self, app_mod, monkeypatch
    ):
        mod = app_mod
        b = asyncio.run(mod.get_browser("chatgpt"))
        b.breaker.fail("captcha", 300)

        async def alive():
            return False

        monkeypatch.setattr(b, "browser_needs_relaunch", alive)

        asked = []

        async def fake_ask(prompt, files=None, task_id=None, client_key=None):
            asked.append(1)
            return "x"

        monkeypatch.setattr(b, "ask", fake_ask)

        out = _body(asyncio.run(
            mod.chat_completions(_FakeReq(_request_body()))))
        assert out.get("error", {}).get("code") == "circuit_open"
        assert asked == []


class TestSessionSlots:
    """A slot is a manual handle on a thread, to separate Hermes sessions."""

    def test_a_plain_name_has_no_slot(self, app_mod):
        assert app_mod.resolve_model("chatgpt") == ("chatgpt", "")

    def test_a_slot_is_split_from_the_brain(self, app_mod):
        assert app_mod.resolve_model("chatgpt#2") == ("chatgpt", "2")

    def test_an_unknown_brain_is_left_alone(self, app_mod):
        assert app_mod.resolve_model("nope#2") == ("nope#2", "")

    def test_slots_are_advertised_as_models(self, app_mod, monkeypatch):
        monkeypatch.setitem(app_mod.CFG, "session_slots", 2)
        ids = list(app_mod.slot_models())
        assert "chatgpt" in ids
        assert "chatgpt#2" in ids
        assert "chatgpt#3" in ids

    def test_different_slots_get_different_task_ids(self, app_mod):
        msgs = [{"role": "user", "content": "hola"}]
        assert app_mod.task_id(msgs, "slot:2") != app_mod.task_id(msgs, "slot:3")
        assert app_mod.task_id(msgs, "slot:2") != app_mod.task_id(msgs)

    def test_a_slot_request_still_routes_to_its_brain(self, app_mod,
                                                      monkeypatch):
        mod = app_mod
        b = asyncio.run(mod.get_browser("chatgpt"))

        async def fake_ask(prompt, files=None, task_id=None, client_key=None):
            return "ok"

        monkeypatch.setattr(b, "ask", fake_ask)
        out = _body(asyncio.run(
            mod.chat_completions(_FakeReq(_request_body(model="chatgpt#2")))))
        assert "choices" in out, out


class TestHelperToggle:
    def test_a_disabled_helper_returns_none(self, app_mod, monkeypatch):
        monkeypatch.setitem(app_mod.CFG["helper"], "enabled", False)
        out = asyncio.run(
            app_mod.call_helper([{"role": "user", "content": "x"}]))
        assert out is None

    def test_the_helper_is_configured_and_on(self, app_mod):
        assert app_mod.CFG["helper"]["enabled"] is True
        assert app_mod.CFG["helper"]["cli_argv"]


class _FakeReq:
    """Minimal stand-in for fastapi Request."""

    def __init__(self, body):
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


def _drain(resp):
    """Collect the body of a StreamingResponse."""
    async def _read():
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return out

    return asyncio.run(_read())


def _body(resp):
    """Return the parsed JSON body of a JSONResponse (or a plain dict)."""
    if isinstance(resp, dict):
        return resp
    raw = resp.body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"error": {"message": raw, "code": "unparseable_response"}}


def _sse_payloads(chunks):
    """Parse SSE chunks into a list of dicts, keeping [DONE] as a marker."""
    out = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        raw = c[len("data: "):].strip()
        if raw == "[DONE]":
            out.append("[DONE]")
            continue
        try:
            out.append(json.loads(raw))
        except Exception:
            out.append({"raw": raw})
    return out
