"""Tests for the brain reply parser.

Pure, no browser, no network. These cover the reply shapes that actually
arrived from the web brains and used to silently degrade into a prose
"final" answer.
"""

import pytest

from brainjson import (
    extract_json_objects,
    normalize_text,
    parse_brain_reply,
    repair_json,
    validate_tool_call,
)

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
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory (takes no arguments)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class TestCleanReplies:
    def test_plain_tool_call(self):
        r = parse_brain_reply('{"tool": "run_cmd", "args": {"command": "ls"}}', TOOLS)
        assert r.kind == "tool"
        assert r.tool == "run_cmd"
        assert r.args == {"command": "ls"}

    def test_final_answer(self):
        r = parse_brain_reply('{"final": "All done."}', TOOLS)
        assert r.kind == "final"
        assert r.final == "All done."

    def test_tool_call_without_args(self):
        r = parse_brain_reply('{"tool": "list_dir"}', TOOLS)
        assert r.kind == "tool"
        assert r.tool == "list_dir"
        assert r.args == {}

    def test_whitespace_and_newlines(self):
        r = parse_brain_reply('\n\n  {"tool": "run_cmd", "args": {"command": "ls"}}  \n',
                              TOOLS)
        assert r.kind == "tool"


class TestMarkdownMangling:
    """Every one of these arrived from a real web brain and used to break."""

    def test_fenced_json(self):
        reply = (
            "Here is the call:\n"
            "```json\n"
            '{"tool": "run_cmd", "args": {"command": "git log --oneline -3"}}\n'
            "```\n"
            "Let me know if you need anything else."
        )
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
        assert r.args["command"] == "git log --oneline -3"

    def test_fenced_json_without_language_tag(self):
        reply = '```\n{"tool": "run_cmd", "args": {"command": "ls"}}\n```'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"

    def test_prose_wrapper(self):
        reply = ('Sure! Here you go: {"tool": "run_cmd", "args": {"command": "ls"}} '
                 "- hope that helps!")
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
        assert r.args["command"] == "ls"

    def test_curly_quotes(self):
        reply = '{“tool”: “run_cmd”, “args”: {“command”: “ls”}}'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
        assert r.args["command"] == "ls"

    def test_html_entities(self):
        reply = "{&quot;tool&quot;: &quot;run_cmd&quot;, &quot;args&quot;: {&quot;command&quot;: &quot;ls&quot;}}"
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"

    def test_trailing_comma(self):
        reply = '{"tool": "run_cmd", "args": {"command": "ls",},}'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
        assert r.args["command"] == "ls"

    def test_literal_newline_in_string_value(self):
        reply = '{"tool": "run_cmd", "args": {"command": "echo a\necho b"}}'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
        assert "echo a" in r.args["command"]

    def test_unterminated_string(self):
        reply = '{"tool": "run_cmd", "args": {"command": "echo hello'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"

    def test_markdown_emphasis_glued_on(self):
        reply = '**{"tool": "run_cmd", "args": {"command": "ls"}**}'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"

    def test_brace_inside_string_value(self):
        """A naive brace scanner breaks on this. A shell command can do it."""
        reply = '{"tool": "run_cmd", "args": {"command": "awk \'{print $1}\' f.txt"}}'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
        assert "awk" in r.args["command"]

    def test_nested_objects(self):
        reply = ('{"tool": "run_cmd", "args": {"command": "ls", "opts": {"deep": [1, 2]}}}')
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"


class TestHallucinatedToolCalls:
    """The brain inventing things must be caught before Hermes runs them."""

    def test_unknown_tool_name_rejected(self):
        r = parse_brain_reply('{"tool": "delete_everything", "args": {}}', TOOLS)
        assert r.kind != "tool"
        assert "unknown_tool" in r.reason

    def test_missing_required_arg_rejected(self):
        r = parse_brain_reply('{"tool": "run_cmd", "args": {}}', TOOLS)
        assert r.kind != "tool"
        assert "missing_arg" in r.reason

    def test_wrong_arg_type_rejected(self):
        r = parse_brain_reply('{"tool": "run_cmd", "args": {"command": 42}}', TOOLS)
        assert r.kind != "tool"
        assert "bad_type" in r.reason

    def test_numeric_string_coerced(self):
        r = parse_brain_reply('{"tool": "run_cmd", "args": {"command": "ls"}}', TOOLS)
        assert r.kind == "tool"

    def test_unparseable_reply_is_not_treated_as_final(self):
        """The core bug: prose used to be handed back as a finished answer."""
        r = parse_brain_reply("I could not run that command, let me try another way.",
                              TOOLS)
        assert r.kind == "unparsed"
        assert r.reason


class TestValidation:
    def test_no_tool_list_accepts_anything(self):
        ok, reason, args = validate_tool_call({"tool": "whatever", "args": {}}, None)
        assert ok and reason == ""

    def test_extra_args_allowed(self):
        ok, _, _ = validate_tool_call(
            {"tool": "run_cmd", "args": {"command": "ls", "timeout": 5}}, TOOLS)
        assert ok

    def test_args_as_json_string(self):
        ok, _, args = validate_tool_call(
            {"tool": "run_cmd", "args": '{"command": "ls"}'}, TOOLS)
        assert ok and args["command"] == "ls"


class TestHelpers:
    def test_normalize_curly_quotes(self):
        assert normalize_text("“hi”") == '"hi"'

    def test_normalize_html_entities(self):
        assert normalize_text("&quot;hi&quot;") == '"hi"'

    def test_repair_returns_none_for_junk(self):
        assert repair_json("not json at all") is None

    def test_extract_finds_multiple(self):
        objs = extract_json_objects('{"a": 1} then {"tool": "x"}')
        assert len(objs) == 2

    def test_tool_call_wins_over_other_objects(self):
        reply = '{"note": "hi"} {"tool": "run_cmd", "args": {"command": "ls"}}'
        r = parse_brain_reply(reply, TOOLS)
        assert r.kind == "tool"
