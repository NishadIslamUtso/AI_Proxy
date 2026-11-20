"""Robust parsing of a web-brain's tool-call reply.

The brain is a chat page, not an API. Its reply comes back through a markdown
renderer, so the JSON we asked for can arrive:

  - inside a ```json fence
  - wrapped in prose ("Sure! Here you go: {...}")
  - with curly quotes ("tool" instead of "tool")
  - with HTML entities (&quot; instead of ")
  - with a trailing comma
  - with a literal newline inside a string value
  - with markdown emphasis glued to the quotes
  - not at all (the brain answered in prose)

`extract_json` in server.py handled none of those except a bare
`{...}`, so a mangled reply silently degraded into a "final" answer. The
framework then believed the brain had finished the task when in fact no
tool ever ran. That is the "terminal command became gibberish" bug and the
"brain answered from memory" bug, and they are the same bug.

This module is dependency-free and pure, so it is unit-testable without a
browser, an account, or a network.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field

__all__ = [
    "BrainReply",
    "extract_json_objects",
    "normalize_text",
    "parse_brain_reply",
    "repair_json",
    "validate_tool_call",
]


# --------------------------------------------------------------------------
# text normalisation
# --------------------------------------------------------------------------

# Curly quotes and dashes are what a markdown renderer hands back when it
# "helpfully" reformats the model's output.
_SMART = {
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "—": "-", "–": "-", "‒": "-", "―": "-",
    " ": " ", " ": " ", " ": " ",
}
_SMART_RE = re.compile("|".join(re.escape(k) for k in _SMART))

_FENCE_RE = re.compile(
    r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)(?:\r?\n)?```",
    re.DOTALL,
)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*)")


def normalize_text(text: str) -> str:
    """Undo the transformations a markdown renderer applies to our JSON."""
    if not text:
        return ""
    out = _SMART_RE.sub(lambda m: _SMART[m.group(0)], text)
    # &quot; &amp; &#39; ... show up when the reply is read back from innerText.
    out = html.unescape(out)
    # Zero-width characters sometimes survive a copy/paste round trip.
    out = out.replace("", "").replace("﻿", "")
    return out


def _strip_fences(text: str):
    """Return the contents of every fenced code block, in order."""
    return [m.group(2) for m in _FENCE_RE.finditer(text)]


def _balanced_spans(text: str):
    """Yield every top-level {...} span, ignoring braces inside strings.

    A naive scanner breaks on {"command": "echo }"} - which is a perfectly
    normal shell command and a perfectly normal thing for the brain to send.
    """
    spans = []
    depth = 0
    start = -1
    in_str = False
    quote = ""
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start:i + 1])
                    start = -1
    return spans


def repair_json(text: str):
    """Best-effort repair of JSON that is nearly - but not quite - valid.

    Handles the specific damage a markdown round trip does:
      - trailing commas before } or ]
      - literal newlines / tabs inside string values
      - a missing closing quote at the end of a string value
    Returns a JSON object, or None if it still will not parse.
    """
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass

    # 1. trailing commas
    s2 = re.sub(r",(\s*[}\]])", r"\1", s)
    try:
        return json.loads(s2)
    except Exception:
        pass

    # 2. control characters inside string values
    s3 = _escape_control_chars(s2)
    try:
        return json.loads(s3)
    except Exception:
        pass

    # 3. unterminated final string
    s4 = _close_open_string(s3)
    try:
        return json.loads(s4)
    except Exception:
        pass

    # 4. single quotes used as JSON quotes
    s5 = _single_to_double(s4)
    try:
        return json.loads(s5)
    except Exception:
        pass

    # 6. last resort: drop the emphasis markers a renderer glued on
    s6 = _EMPHASIS_RE.sub("", s5)
    try:
        return json.loads(s6)
    except Exception:
        return None


def _escape_control_chars(s: str) -> str:
    """Escape raw newlines/tabs that sit inside a JSON string value."""
    out = []
    in_str = False
    escaped = False
    for ch in s:
        if in_str:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _close_open_string(s: str) -> str:
    """Close a string value the renderer left open, then close the object."""
    in_str = False
    escaped = False
    for ch in s:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
    if not in_str:
        return s
    # We are inside a string. Close it, then close whatever containers are open.
    depth = 0
    in_str2 = False
    escaped2 = False
    for ch in s:
        if in_str2:
            if escaped2:
                escaped2 = False
            elif ch == "\\":
                escaped2 = True
            elif ch == '"':
                in_str2 = False
            continue
        if ch == '"':
            in_str2 = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
    out = s + '"'
    out += "}" * depth
    return out


def _single_to_double(s: str) -> str:
    """Convert single-quoted keys/values to double-quoted, JSON style."""
    out = []
    in_dq = False
    in_sq = False
    escaped = False
    for ch in s:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            out.append(ch)
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            out.append('"')
            continue
        out.append(ch)
    return "".join(out)


def extract_json_objects(text: str):
    """Return every JSON object we can find in `text`, best candidate first.

    Order of attempts:
      1. the whole reply
      2. each fenced code block
      3. each balanced {...} span
    Each attempt is tried raw, then normalized, then repaired.
    """
    if not text:
        return []
    found = []
    seen = set()

    def _add(obj):
        if not isinstance(obj, dict):
            return
        key = json.dumps(obj, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            found.append(obj)

    sources = [text]
    sources.extend(_strip_fences(text))
    for src in sources:
        _add(repair_json(src))
        norm = normalize_text(src)
        _add(repair_json(norm))
        for span in _balanced_spans(norm):
            _add(repair_json(span))

    # Objects that carry a brain decision come first.
    def _rank(obj):
        if "tool" in obj:
            return 0
        if "final" in obj:
            return 1
        if "tool_calls" in obj:
            return 2
        if "args" in obj or "arguments" in obj:
            return 3
        return 4

    found.sort(key=_rank)
    return found


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _tool_specs(tools):
    """Normalise the request's `tools` array into {name: schema}."""
    specs = {}
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        specs[name] = params if isinstance(params, dict) else {}
    return specs


def validate_tool_call(obj, tools):
    """Check a parsed tool call against the tool list Hermes offered.

    Returns (ok, reason, normalised_args). `reason` is empty when ok.

    A brain that invents a tool name, or asks for an argument the schema does
    not define, gets rejected here - before Hermes tries to run it and
    produces a confusing failure much further down the stack.
    """
    if not isinstance(obj, dict):
        return False, "not_an_object", {}

    name = obj.get("tool")
    if not isinstance(name, str) or not name.strip():
        return False, "missing_tool_name", {}
    name = name.strip()

    specs = _tool_specs(tools)
    if specs and name not in specs:
        return False, f"unknown_tool:{name}", {}
    if not specs:
        # No schema available (e.g. a plain chat). Accept the call as-is.
        args = obj.get("args")
        return True, "", args if isinstance(args, dict) else {}

    schema = specs.get(name) or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []

    raw_args = obj.get("args", obj.get("arguments"))
    if raw_args is None:
        args = {}
    elif isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except Exception:
            return False, "args_not_json", {}
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        return False, "args_not_object", {}
    if not isinstance(args, dict):
        return False, "args_not_object", {}

    for key in required:
        if key not in args:
            return False, f"missing_arg:{key}", args

    for key, value in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        want = spec.get("type")
        if not isinstance(want, str):
            continue
        expected = _TYPE_MAP.get(want)
        if expected is None:
            continue
        if want == "integer" and isinstance(value, bool):
            return False, f"bad_type:{key}", args
        if want == "number" and isinstance(value, bool):
            return False, f"bad_type:{key}", args
        if not isinstance(value, expected):
            # A number written as a string is a very common brain slip and
            # is safe to coerce.
            if want in ("integer", "number") and isinstance(value, str):
                try:
                    args[key] = int(value) if want == "integer" else float(value)
                    continue
                except ValueError:
                    pass
            if want == "boolean" and isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "false"):
                    args[key] = low == "true"
                    continue
            return False, f"bad_type:{key}", args

    return True, "", args


# --------------------------------------------------------------------------
# the reply object
# --------------------------------------------------------------------------

@dataclass
class BrainReply:
    """What the brain actually said, parsed as far as we can manage."""

    kind: str = "unparsed"          # tool | final | unparsed
    tool: str = ""
    args: dict = field(default_factory=dict)
    final: str = ""
    reason: str = ""                 # why it is unparsed
    raw: str = ""
    candidates: int = 0

    @property
    def ok(self) -> bool:
        return self.kind in ("tool", "final")

    def as_tool_call(self):
        """OpenAI-shaped tool_calls payload, or None."""
        if self.kind != "tool":
            return None
        return [{
            "id": f"call_{abs(hash((self.tool, self.raw))) & 0xFFFFFFFF:08x}",
            "type": "function",
            "function": {
                "name": self.tool,
                "arguments": json.dumps(self.args),
            },
        }]


def parse_brain_reply(reply: str, tools=None) -> BrainReply:
    """Parse a raw brain reply into a tool call, a final answer, or a failure.

    `tools` is the request's tool list; when given, a tool call is validated
    against it, so a hallucinated tool name or a malformed argument is
    reported as a parse failure instead of being passed downstream.
    """
    raw = reply or ""
    out = BrainReply(raw=raw)

    objs = extract_json_objects(normalize_text(raw))
    out.candidates = len(objs)

    # 1. a tool call
    for obj in objs:
        if "tool" not in obj:
            continue
        ok, reason, args = validate_tool_call(obj, tools)
        if ok:
            out.kind = "tool"
            out.tool = str(obj["tool"]).strip()
            out.args = args
            return out
        # Remember the first rejection - it is the most useful diagnostic.
        if not out.reason:
            out.reason = reason

    # 2. a final answer
    for obj in objs:
        if "final" in obj and isinstance(obj["final"], str):
            out.kind = "final"
            out.final = obj["final"]
            return out

    # 3. nothing structured at all
    if not out.reason:
        out.reason = "no_json_object"
    return out


def looks_like_prose(reply: str) -> bool:
    """True when the reply is not a tool call we can use."""
    return "tool" not in (extract_json_objects(normalize_text(reply or ""))[0]
                          if extract_json_objects(normalize_text(reply or "")) else {})
