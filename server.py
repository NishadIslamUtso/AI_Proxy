"""brain-proxy: a local OpenAI-compatible endpoint backed by a web chat AI.

Hermes points its model at http://127.0.0.1:8000/v1 and every "model" call is
answered by a real web chat (ChatGPT, Arena, DeepSeek, z.ai) driven in a
dedicated browser profile.

The contract with the brain is strict on purpose: when the caller offers
tools, the brain must answer with a tool call or an explicit final answer.
Prose that merely claims a command result is refused rather than passed
through, because a made-up tool result is worse than a loud failure.
"""
import asyncio
import base64
import hashlib
import json
import re
import socket
import time
import uuid
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from brainjson import parse_brain_reply
from brainjson import extract_json_objects as brainjson_objects
from browser import BRAIN_PREAMBLE, BRAIN_PREAMBLE_PLAIN, BrainBrowser
from errors import (
    COOLDOWN,
    FAIL_FAST,
    TRANSIENT,
    BrainError,
    as_openai_error,
    classify,
)

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

app = FastAPI(title="brain-proxy")
BROWSERS = {}
STATE = {}
_FACTORY_LOCK = asyncio.Lock()

TELEMETRY = ROOT / "telemetry.jsonl"
STATS = {}
MAX_CONCURRENT = int(CFG.get("max_concurrent", 2))
_ACTIVE = asyncio.Semaphore(MAX_CONCURRENT)
_ACTIVE_N = {"n": 0}


def record(brain, code, ms, attempt=0, ok=False):
    try:
        with TELEMETRY.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "brain": brain,
                                "code": code, "ms": int(ms), "attempt": attempt,
                                "ok": ok}) + "\n")
    except Exception:
        pass
    s = STATS.setdefault(brain, {"ok": 0, "fail": 0, "codes": {}})
    if ok:
        s["ok"] += 1
    else:
        s["fail"] += 1
        s["codes"][code] = s["codes"].get(code, 0) + 1

import os as _os
import subprocess as _subprocess

HELPER_RECURSION_GUARD = "HERMES_PROXY_GUARD"


async def call_helper(messages, tools=None, kind="chat"):
    h = CFG.get("helper") or {}
    if not h.get("enabled"):
        return None
    if _os.environ.get(HELPER_RECURSION_GUARD):
        print("[helper] recursion guard active, refusing to call", flush=True)
        return None

    system_text = h.get("role_prompt", "")
    if kind == "tool_json" and tools:
        system_text = h.get("tool_format_prompt", "") or system_text

    flat = [{"role": "system", "content": system_text}]
    for m in messages:
        c = content_to_text(m.get("content", ""))
        if c:
            flat.append({"role": m.get("role", "user"), "content": c})
    if kind == "tool_json" and tools:
        tool_list = "\n".join(
            f"- {t['function']['name']}: {t['function'].get('description','')}"
            for t in tools if t.get("type") == "function"
        )
        flat.append({"role": "user", "content":
                     "Available tools:\n" + tool_list +
                     "\n\nReturn exactly one JSON object."})

    prompt = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in flat)

    argv = [str(a).replace("{prompt}", prompt) for a in (h.get("cli_argv") or [])]
    if not argv:
        print("[helper] no cli_argv configured", flush=True)
        return None

    env = _os.environ.copy()
    env.update(h.get("cli_env") or {})

    timeout = float(h.get("cli_timeout_seconds", 90))
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout, env=env,
            ),
        )
    except _subprocess.TimeoutExpired:
        print("[helper] timeout", flush=True)
        return None
    except FileNotFoundError:
        print("[helper] hermes binary not found on PATH", flush=True)
        return None
    except Exception as e:
        print(f"[helper] spawn failed: {type(e).__name__}: {e}", flush=True)
        return None

    if result.returncode != 0:
        print(f"[helper] exit {result.returncode}: "
              f"{(result.stderr or '').strip()[:200]}", flush=True)
        return None

    out = (result.stdout or "").strip()
    if not out:
        print("[helper] empty stdout", flush=True)
        return None

    if "<html" in out.lower() or "brain-proxy" in out.lower():
        print("[helper] output looks like proxy echo, refusing", flush=True)
        return None

    print(f"[helper] ok kind={kind} len={len(out)}", flush=True)
    return out


def reply_looks_like_tool_json(reply, tools=None):
    """True when the reply carries a usable tool call or an explicit final.

    The old version required the reply to *start* with "{", so a brain that
    wrote "Sure! here you go: {...}" was judged unusable and the good reply
    was thrown away.
    """
    if not (reply or "").strip():
        return False
    return parse_brain_reply(reply, tools).kind in ("tool", "final")


ROLE_MARKERS = (
    "[SYSTEM]", "[USER]", "[ASSISTANT]", "[TOOL RESULT]",
    "--- CONVERSATION ---", "--- END ---", "[BRAIN PROXY]",
)


def err(status, message, typ="invalid_request_error", code=None):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": typ, "code": code}},
    )


def content_to_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" or "text" in part:
                out.append(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                out.append("[image omitted - this brain cannot see images]")
        return "\n".join(x for x in out if x)
    return str(content)


def sanitize(text):
    for m in ROLE_MARKERS:
        text = text.replace(m, m.replace("[", "(").replace("]", ")"))
    return text


def truncate_tool(name, text, limits):
    limit = limits.get(name, limits.get("default", 8000))
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n[...{omitted} chars omitted...]\n{tail}"


def format_message(m, limits):
    role = m.get("role", "user")
    if m.get("tool_calls"):
        calls = []
        for tc in m["tool_calls"]:
            fn = tc.get("function", {})
            calls.append(f"[ASSISTANT TOOL CALL id={tc.get('id')} "
                         f"{fn.get('name')}({fn.get('arguments')})]")
        return "\n".join(calls)
    text = content_to_text(m.get("content")).strip()
    if not text:
        return ""
    if role == "system":
        # Hermes's system prompt is written for a tool-capable model and is far
        # too large/noisy for a web chat. The preamble + tools block carry the
        # role; keep only a compact statement.
        return "[SYSTEM] You are the assistant in an agent framework. The framework handles tools, files and execution - you reason and reply."
    if role == "tool":
        name = m.get("name", "")
        text = truncate_tool(name, text, limits)
        return f"[TOOL RESULT id={m.get('tool_call_id', '?')}]\n{text}"
    return f"[{role.upper()}]\n{text}"


def format_conversation(messages, limits):
    return "\n\n".join(
        block for block in (format_message(m, limits) for m in messages) if block
    )


def task_id(messages):
    """A stable id for the *task* behind a request.

    The prompt we send changes on every turn (it carries the conversation),
    so hashing the prompt produced a different key each time and the
    per-task -> thread map never matched. The original task does not change,
    so that is what identifies the task.
    """
    first = first_user_message(messages)
    return hashlib.sha256(first[:400].encode("utf-8", "ignore")).hexdigest()[:16]


def first_user_message(messages):
    for m in messages:
        if m.get("role") == "user":
            t = content_to_text(m.get("content")).strip()
            if t:
                return t
    return ""


ATTACH_DIR = Path("C:/hermes/brain-proxy/attachments")


def extract_files(messages):
    """Pull base64 images out of content parts and write them to disk."""
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for part in c:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url", "")
            if not isinstance(url, str) or not url.startswith("data:"):
                continue
            header, _, b64 = url.partition(",")
            ext = "png"
            mm = re.search(r"data:image/(\w+);", header)
            if mm:
                ext = mm.group(1)
            p = ATTACH_DIR / f"attach_{len(paths)}.{ext}"
            try:
                p.write_bytes(base64.b64decode(b64))
                paths.append(str(p))
            except Exception:
                continue
    return paths


def tool_name(tool):
    """The name of an OpenAI-shaped tool entry."""
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    if isinstance(fn, dict):
        return fn.get("name")
    return tool.get("name")


def advertised_tools(brain_name, tools):
    """The subset of tools this brain is offered.

    A brain that runs in its own sandbox (arena-agent has a workspace and a
    shell) should do that work itself rather than round-tripping every command
    through Hermes, which costs the user tokens and device time. So a brain can
    be restricted to the tools that genuinely need the user's device.

    Unset means "offer everything" - right for a brain with no workspace of its
    own, which can only act through Hermes.
    """
    allowed = (CFG["brains"].get(brain_name) or {}).get("advertise_tools")
    if allowed is None:
        return list(tools or [])
    keep = set(allowed)
    return [t for t in (tools or []) if tool_name(t) in keep]


def tools_block(tools, max_chars=8000):
    if not tools:
        return ""
    lines = ["[FRAMEWORK TOOLS AVAILABLE]"]
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name")
        desc = (fn.get("description") or "").strip().split("\n")[0][:120]
        params = fn.get("parameters") or {}
        props = list((params.get("properties") or {}).keys())
        req = params.get("required") or []
        arg_hint = ",".join(props[:8]) + ("|*" + ",".join(req[:4]) if req else "")
        lines.append(f"- {name}({arg_hint}): {desc}")
    lines.append(
        'Reply with exactly one JSON object and nothing else: '
        '{"tool": "<name>", "args": {...}} or {"final": "<answer>"}.'
    )
    block = "\n".join(lines) + "\n"
    if len(block) > max_chars:
        block = block[:max_chars] + "\n[...tool list truncated...]\n"
    return block


def extract_json(reply):
    """Best-effort: find a JSON object in a brain reply.

    Kept for callers that just want "the object the brain sent". The
    tool/final decision is made by parse_tool_reply below, which also
    validates the result against the offered tools.
    """
    objs = brainjson_objects(reply)
    return objs[0] if objs else None


def parse_tool_reply(reply, tools=None):
    """Turn a brain reply into an OpenAI-shaped tool call or a final answer.

    Unlike the old implementation this never invents a result: a reply that
    cannot be parsed into a tool call or an explicit {"final": ...} comes
    back with an "error" key instead of being passed through as a finished
    answer.
    """
    parsed = parse_brain_reply(reply, tools)
    if parsed.kind == "tool":
        return {"tool_calls": parsed.as_tool_call()}
    if parsed.kind == "final":
        return {"final": parsed.final}
    return {"final": None, "error": parsed.reason, "raw": parsed.raw}


async def get_browser(name):
    async with _FACTORY_LOCK:
        if name not in BROWSERS:
            if name not in CFG["brains"]:
                raise KeyError(name)
            b = BrainBrowser(name, CFG["brains"][name], CFG["browser"],
                             CFG.get("recovery"))
            b.max_turns = CFG.get("max_turns_per_thread", 40)
            b.reanchor_every = CFG.get("reanchor_every", 10)
            BROWSERS[name] = b
    return BROWSERS[name]


def build_prompt(name, messages, tools, limits):
    st = STATE.setdefault(name, {"turns": 0, "schema": "", "tail": ""})
    conv = format_conversation(messages, limits)
    tools_txt = tools_block(tools) if tools else ""
    schema = hashlib.sha256(
        json.dumps(tools or [], sort_keys=True).encode()
    ).hexdigest()

    first = first_user_message(messages)
    tail_src = ""
    if messages:
        last = messages[-1]
        tail_src = str(last.get("role", "")) + ":" + content_to_text(
            last.get("content", "")
        )[-400:]
    tail = hashlib.sha256(tail_src.encode()).hexdigest()

    new_msgs = messages[st["turns"]:] if len(messages) > st["turns"] else []

    # Full resend when: first call, tool schema changed, the list shrank
    # (compaction), or the shape is unchanged but the task itself changed.
    full = (
        st["turns"] == 0
        or st["schema"] != schema
        or len(messages) < st["turns"]
        or not new_msgs
    )

    if full:
        head = (BRAIN_PREAMBLE if tools_txt else BRAIN_PREAMBLE_PLAIN) + tools_txt
        if first:
            head += f"\n[ORIGINAL TASK]\n{first[:2000]}\n"
        prompt = head + "\n--- CONVERSATION ---\n" + conv + "\n--- END ---"
    else:
        prompt = "\n\n".join(
            block for block in
            (format_message(m, limits) for m in new_msgs) if block
        )
        if not prompt:
            prompt = "Continue."
        if tools_txt:
            # the preamble is only sent on the first turn; later turns still
            # need the tool list or the brain stops emitting tool calls
            prompt = tools_block(tools) + "\n" + prompt
        if st["turns"] and st["turns"] % CFG.get("reanchor_every", 10) == 0:
            prompt = ("[REMINDER: you are the reasoning engine inside an agent "
                      "framework. No files. One JSON per tool action.]\n\n" + prompt)

    st["turns"] = len(messages)
    st["schema"] = schema
    st["tail"] = tail
    return prompt


async def brain_call(name, prompt, max_wait=None, files=None, task_id=None):
    """Call a brain, escalating through the configured fallback list."""
    if max_wait is None:
        max_wait = int(CFG.get("max_wait_seconds", 180))
    _bcfg = CFG["brains"].get(name) or {}
    if _bcfg.get("mode") == "agent":
        max_wait = int(_bcfg.get("agent_deadline", 7200))
    if CFG.get("escalation_enabled", False):
        order = [name] + [b for b in CFG.get("escalation", []) if b != name]
    else:
        order = [name]
    last = None
    for brain in order:
        if brain not in CFG["brains"]:
            continue
        b = await get_browser(brain)
        if getattr(b, "paused", False):
            if brain == name:
                raise BrainError("paused", f"{brain} is paused by user", brain)
            last = BrainError("paused_skipped", f"{brain} paused", brain)
            print(f"[brain-proxy] skip paused escalation brain {brain}", flush=True)
            continue
        if not b.breaker.allow():
            # A dead browser is an infrastructure fault, not a brain fault.
            # If we leave the breaker open, brain_call never reaches
            # _connect, so the browser is never relaunched and the brain stays
            # benched no matter how many cooldowns pass.
            if await b.browser_needs_relaunch():
                print(f"[brain-proxy] {brain}: circuit {b.breaker.as_dict()['state']} "
                      f"but the browser is gone; resetting it", flush=True)
                b.breaker.ok()
            else:
                st = b.breaker.as_dict()
                print(f"[brain-proxy] {brain} skipped: circuit {st['state']} "
                      f"({st['last_code']})", flush=True)
                last = BrainError("circuit_open", f"{brain} is benched", brain)
                continue
        t0 = time.time()
        try:
            reply = await asyncio.wait_for(
                b.ask(prompt, files=files, task_id=task_id), timeout=max_wait)
            record(brain, "ok", (time.time() - t0) * 1000, ok=True)
            return reply, brain
        except BrainError as e:
            last = e
            record(brain, e.code, (time.time() - t0) * 1000, ok=False)
            print(f"[brain-proxy] {brain} [{e.code}]: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            code = classify(e)
            last = BrainError(code, f"{type(e).__name__}: {e}", brain)
            record(brain, code, (time.time() - t0) * 1000, ok=False)
            print(f"[brain-proxy] {brain} [{code}]: {e}", flush=True)
    if last is not None:
        raise last
    raise BrainError("proxy_escalation_failed", f"tried {order}")


async def resolve_reply(brain, served, reply, messages, tools, tid=None):
    """Turn a raw brain reply into a decision we can trust.

    Returns a BrainReply with kind "tool" or "final".

    When the request offered tools, the brain MUST answer with either a
    tool call or an explicit {"final": ...}. Prose is not an acceptable
    substitute: a brain that says "the command returned 42" without a tool
    call is claiming a result that no tool produced. Handing that back as a
    finished answer is exactly the failure this whole project exists to
    avoid, so instead we:

      1. ask the brain once more, with a stricter instruction,
      2. fall back to the helper CLI if one is configured,
      3. and only then fail loudly with a diagnostic.

    With no tools offered, plain prose IS the answer and is returned as-is.
    """
    parsed = parse_brain_reply(sanitize(reply), tools)

    if parsed.kind in ("tool", "final"):
        return parsed
    if not tools:
        # No tools were offered, so there is no protocol to violate.
        parsed.kind = "final"
        parsed.final = reply
        return parsed

    reason = parsed.reason or "no_json_object"
    print(f"[brain-proxy] reply not parseable ({reason}); retrying", flush=True)

    # 1. one stricter retry against the same brain
    retry_prompt = (
        "Your previous reply could not be parsed. Reply with exactly one "
        "JSON object and nothing else - no prose, no markdown fence, no "
        "explanation before or after:\n"
        '{"tool": "<tool_name>", "args": {...}}\n'
        'or\n'
        '{"final": "<your answer>"}\n'
        "If a tool failed, still return the tool call so the framework can "
        "run it and show you the real output. Never state a command result "
        "you did not receive from a tool.\n"
        "Task to continue: " + content_to_text(messages[-1].get("content", ""))
    )
    try:
        _r = await asyncio.wait_for(
            brain_call(served or brain, retry_prompt, task_id=tid),
            timeout=60,
        )
        retry_reply = _r[0] if isinstance(_r, tuple) else _r
    except Exception as e:  # noqa: BLE001
        print(f"[brain-proxy] retry failed: {type(e).__name__}: {e}", flush=True)
        retry_reply = None

    if retry_reply:
        parsed = parse_brain_reply(sanitize(retry_reply), tools)
        if parsed.kind in ("tool", "final"):
            return parsed
        reason = parsed.reason or reason

    # 2. helper CLI fallback
    print("[brain-proxy] tool JSON still missing, delegating to helper",
          flush=True)
    _h = await call_helper(messages, tools=tools, kind="tool_json")
    if _h is not None:
        parsed = parse_brain_reply(sanitize(_h), tools)
        if parsed.kind in ("tool", "final"):
            return parsed
        reason = parsed.reason or reason

    # 3. fail loudly - never invent a result
    raise BrainError(
        "unparseable_brain_reply",
        f"brain '{brain}' produced a reply that is neither a tool call nor a "
        f"final answer (reason={reason}); refusing to pass prose through as "
        f"a completed result",
        brain,
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "brains": list(BROWSERS.keys()),
        "config": list(CFG["brains"]),
        "circuits": {n: b.breaker.as_dict() for n, b in BROWSERS.items()},
        "active": _ACTIVE_N["n"],
        "max_concurrent": MAX_CONCURRENT,
    }


@app.post("/debug/reset-session")
async def reset_session(request: Request):
    body = await request.json()
    name = body.get("brain")
    if not name:
        return JSONResponse(status_code=400, content={"error": "brain required"})
    if name not in BROWSERS:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    b = BROWSERS[name]
    b.session_url = ""
    b._save_learned()
    return {"ok": True, "brain": name, "session_url": ""}


@app.get("/debug/sessions")
async def list_sessions():
    out = {}
    for name, b in BROWSERS.items():
        out[name] = {
            "session_url": getattr(b, "session_url", ""),
            "learned_input": getattr(b, "learned_input_selectors", []),
            "learned_response": getattr(b, "learned_response_selectors", []),
        }
    return out


@app.get("/debug/tabs")
async def list_tabs(brain: str):
    if brain not in BROWSERS:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    b = BROWSERS[brain]
    try:
        await b._connect()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    out = []
    for p in b._context.pages:
        try:
            alive = await b._page_alive(p)
        except Exception:
            alive = False
        out.append({"url": p.url, "alive": alive, "current": p is b._page})
    return {"brain": brain, "session_url": getattr(b, "session_url", ""), "tabs": out}


@app.get("/debug/google")
async def debug_google(brain: str):
    if brain not in BROWSERS:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    b = BROWSERS[brain]
    try:
        await b._connect()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    page = await b._context.new_page()
    try:
        await page.goto("https://myaccount.google.com/", wait_until="domcontentloaded",
                        timeout=20000)
        await asyncio.sleep(3)
        url = page.url
        body = (await page.evaluate("document.body.innerText"))[:300]
        logged_in = ("myaccount.google.com" in url
                     and "sign in" not in body.lower()
                     and "signin" not in url.lower())
        return {"brain": brain, "url": url, "logged_in": logged_in,
                "snippet": body[:200]}
    finally:
        await page.close()


@app.get("/debug/tool-test")
async def tool_test(brain: str):
    if brain not in CFG["brains"]:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    messages = [{
        "role": "user",
        "content": "What is the weather in Tokyo? Use the get_weather tool.",
    }]
    try:
        b = await get_browser(brain)
        prompt = build_prompt(brain, messages, tools, CFG.get("tool_truncation", {}))
        reply = await asyncio.wait_for(b.ask(prompt), timeout=300)
        return {
            "brain": brain,
            "looks_like_json": reply_looks_like_tool_json(reply),
            "reply_head": reply[:300],
        }
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})


@app.post("/debug/pause")
async def pause_brain(request: Request):
    body = await request.json()
    name = body.get("brain")
    if name not in CFG["brains"]:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    b = await get_browser(name)
    b.paused = True
    b.breaker.fail("paused", COOLDOWN.get("paused", 3600))
    return {"ok": True, "brain": name, "paused": True,
            "current_url": b.session_url or b.url}


@app.post("/debug/adopt")
async def adopt_tab(request: Request):
    from urllib.parse import urlparse
    body = await request.json()
    name = body.get("brain")
    if name not in CFG["brains"]:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    b = await get_browser(name)
    await b._connect()
    host = urlparse(b.url).netloc
    adopted = None
    for p in b._context.pages:
        try:
            if p.url.startswith("http") and host in p.url and await b._page_alive(p):
                adopted = p
                break
        except Exception:
            continue
    if adopted is None:
        return JSONResponse(status_code=404,
                            content={"error": f"no live tab on {host}"})
    b._page = adopted
    b.session_url = adopted.url
    b.url = adopted.url
    b.paused = False
    b.breaker.ok()
    b._save_learned()
    return {"ok": True, "brain": name, "paused": False,
            "adopted_url": adopted.url, "breaker": "closed"}


@app.get("/debug/state")
async def debug_state(brain: str):
    if brain not in CFG["brains"]:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    b = BROWSERS.get(brain)
    if b is None:
        return {"brain": brain, "instantiated": False}
    return {
        "brain": brain,
        "instantiated": True,
        "paused": getattr(b, "paused", False),
        "url": b.url,
        "session_url": b.session_url,
        "breaker": b.breaker.as_dict(),
    }


@app.get("/debug/helper-test")
async def helper_test():
    h = CFG.get("helper") or {}
    if not h.get("enabled"):
        return {"enabled": False, "reason": "helper.enabled is false"}
    reply = await call_helper(
        [{"role": "user", "content": "Reply with exactly: HELPER OK"}],
        kind="chat",
    )
    return {"enabled": True, "mode": h.get("mode"),
            "cli_argv": h.get("cli_argv"), "reply": reply}


@app.get("/debug/helper-tool-test")
async def helper_tool_test():
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    reply = await call_helper(
        [{"role": "user", "content":
          "What is the weather in Tokyo? Use the get_weather tool."}],
        tools=tools, kind="tool_json",
    )
    return {"reply": reply}


@app.get("/debug/selfcheck")
async def selfcheck(brain: str = None):
    """Prove each brain can still complete a real turn."""
    names = [brain] if brain else list(CFG["brains"].keys())
    out = {}
    for n in names:
        try:
            b = await get_browser(n)
        except Exception as e:  # noqa: BLE001
            out[n] = {"ok": False, "code": "unknown", "error": str(e)[:200]}
            continue
        t0 = time.time()
        try:
            reply = await asyncio.wait_for(
                b.ask("Reply with exactly: SELFCHECK OK"), timeout=120)
            ok = bool(reply and "SELFCHECK" in reply.upper())
            out[n] = {"ok": ok, "ms": int((time.time() - t0) * 1000),
                      "reply": (reply or "")[:120]}
            record(n, "ok" if ok else "site_changed",
                   (time.time() - t0) * 1000, ok=ok)
            if not ok:
                b.breaker.fail("site_changed", COOLDOWN.get("site_changed"))
        except Exception as e:  # noqa: BLE001
            code = classify(e)
            out[n] = {"ok": False, "code": code, "error": str(e)[:200]}
            b.breaker.fail(code, COOLDOWN.get(code))
            record(n, code, (time.time() - t0) * 1000, ok=False)
    return out


@app.get("/debug/stats")
async def stats():
    return {
        "stats": STATS,
        "circuits": {n: b.breaker.as_dict() for n, b in BROWSERS.items()},
        "active": _ACTIVE_N["n"],
        "max_concurrent": MAX_CONCURRENT,
    }


@app.post("/debug/switch")
async def switch_default(request: Request):
    body = await request.json()
    name = body.get("brain")
    if name not in CFG["brains"]:
        return JSONResponse(status_code=404, content={"error": "unknown brain"})
    CFG["default"] = name
    try:
        import yaml as _y, pathlib as _p
        f = _p.Path(__file__).resolve().parent / "config.yaml"
        data = _y.safe_load(f.read_text(encoding="utf-8"))
        data["default"] = name
        f.write_text(_y.safe_dump(data, sort_keys=False), encoding="utf-8")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"write failed: {e}"})
    return {"ok": True, "default": name}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": n, "object": "model", "owned_by": "brain-proxy"}
                 for n in CFG["brains"]],
    }


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    if model_id not in CFG["brains"]:
        return err(404, f"model '{model_id}' not found", code="model_not_found")
    return {"id": model_id, "object": "model", "owned_by": "brain-proxy"}


@app.get("/debug/dump")
async def debug_dump(brain: str = None, sel: str = None):
    name = brain or CFG["default"]
    try:
        b = await get_browser(name)
        await b._connect()
        if sel:
            data = await b._page.evaluate(
                """(s) => [...document.querySelectorAll(s)].slice(-12).map(e => ({
                    tag: e.tagName,
                    cls: (typeof e.className === 'string') ? e.className : '',
                    len: (e.innerText || '').length,
                    txt: (e.innerText || '').trim().slice(0, 120)
                }))""", sel)
            return {"url": b._page.url, "sel": sel, "matches": data}
        data = await b._page.evaluate("""() => ({
            url: location.href,
            inputs: [...document.querySelectorAll('textarea,input,[contenteditable]')]
                      .map(e => ({tag: e.tagName, id: e.id,
                                  cls: (typeof e.className === 'string')
                                       ? e.className : (e.className?.baseVal || ''),
                                  ph: e.placeholder || ''})),
            bubbles: [...document.querySelectorAll('article,[class*=message],[class*=markdown]')]
                       .slice(-8)
                       .map(e => ({tag: e.tagName,
                                   cls: (typeof e.className === 'string')
                                        ? e.className : (e.className?.baseVal || '')}))
        })""")
        return data
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _completion(response_id, model, message, finish, usage):
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return err(400, "invalid json body")

    messages = body.get("messages") or []
    if not messages:
        return err(400, "messages required")

    model_name = body.get("model") or CFG.get("default") or ""
    if model_name not in CFG["brains"]:
        return err(404, f"model '{model_name}' not found", code="model_not_found")

    stream = bool(body.get("stream"))
    limits = CFG.get("tool_truncation", {})
    files = extract_files(messages)
    tools = advertised_tools(model_name, body.get("tools"))
    prompt = build_prompt(model_name, messages, tools, limits)
    tid = task_id(messages)
    print(f"[brain-proxy] -> {model_name} msgs={len(messages)} "
          f"tools={len(tools)} files={len(files)} "
          f"prompt_len={len(prompt)}", flush=True)
    print(f"[brain-proxy] prompt head: {prompt[:300]!r}", flush=True)
    try:
        (ROOT / "last-tools-block.txt").write_text(
            tools_block(tools), encoding="utf-8"
        )
        (ROOT / "last-request.json").write_text(
            json.dumps({"tools": tools, "messages": messages})[:200000],
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[brain-proxy] dump failed: {e}", flush=True)

    try:
        await asyncio.wait_for(_ACTIVE.acquire(), timeout=120)
    except asyncio.TimeoutError:
        return err(429, f"proxy busy ({MAX_CONCURRENT} in flight, queue full)",
                   typ="rate_limit_error", code="rate_limited")

    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    async def run_brain():
        b = await get_browser(model_name)
        if b.wants_rotation():
            await b.rotate(task_id=tid)
            STATE[model_name]["schema"] = ""
            prompt2 = build_prompt(model_name, messages, tools, limits)
            return await asyncio.wait_for(
                b.ask(prompt2, files=files, task_id=tid), timeout=300)
        return await brain_call(model_name, prompt, files=files, task_id=tid)

    if stream:
        async def gen():
            # The semaphore was acquired before we got here. Streaming used to
            # return without releasing it, so after MAX_CONCURRENT streamed
            # requests the proxy refused every further call with "proxy busy".
            try:
                head = {
                    "id": response_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant", "content": ""},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(head)}\n\n"

                task = asyncio.create_task(run_brain())
                while not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=15)
                        break
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"

                try:
                    reply, served = task.result()
                except BrainError as e:
                    err = {"error": {"message": str(e), "type": "brain_proxy_error",
                                     "code": e.code}}
                    yield f"data: {json.dumps(err)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                except Exception as e:
                    err = {"error": {"message": str(e), "type": "proxy_error"}}
                    yield f"data: {json.dumps(err)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                try:
                    decision = await resolve_reply(
                        model_name, served, reply, messages, tools, tid
                    )
                except BrainError as e:
                    err = {"error": {"message": str(e), "type": "brain_proxy_error",
                                     "code": e.code}}
                    yield f"data: {json.dumps(err)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                if decision.kind == "tool":
                    for tc in decision.as_tool_call():
                        chunk = {
                            "id": response_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {"tool_calls": [{
                                "index": 0, "id": tc["id"], "type": "function",
                                "function": {"name": tc["function"]["name"],
                                             "arguments": tc["function"]["arguments"]},
                            }]}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    tail = {
                        "id": response_id, "object": "chat.completion.chunk",
                        "created": created, "model": model_name,
                        "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "tool_calls"}],
                    }
                    yield f"data: {json.dumps(tail)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                text = decision.final
                if not isinstance(text, str):
                    text = reply
                for i in range(0, len(text), 200):
                    chunk = {
                        "id": response_id, "object": "chat.completion.chunk",
                        "created": created, "model": model_name,
                        "choices": [{"index": 0,
                                     "delta": {"content": text[i:i+200]},
                                     "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.02)
                tail = {
                    "id": response_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(tail)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                _ACTIVE.release()

        return StreamingResponse(gen(), media_type="text/event-stream")

    _mode = (CFG["brains"].get(model_name) or {}).get("mode", "chat")
    _ht = int(CFG.get("hard_timeout_seconds", 300))
    if _mode == "agent":
        _ht = int((CFG["brains"].get(model_name) or {}).get("agent_deadline", 7200)) + 300

    try:
        reply, served = await asyncio.wait_for(
            run_brain(),
            timeout=_ht,
        )
    except BrainError as e:
        _ACTIVE.release()
        status = {"rate_limited": 429, "captcha": 409, "auth_wall": 409,
                  "user_tab_busy": 409, "paused": 409,
                  "consent_wall": 409}.get(e.code, 502)
        return JSONResponse(status_code=status,
                            content=as_openai_error(e.code, str(e)))
    except Exception as e:  # noqa: BLE001
        _ACTIVE.release()
        code = classify(e)
        return JSONResponse(status_code=502,
                            content=as_openai_error(code, str(e)))

    # The slot is held across the retry, because the retry is browser work
    # too and must be counted against the concurrency limit.
    try:
        decision = await resolve_reply(
            model_name, served, reply, messages, tools, tid
        )
    except BrainError as e:
        _ACTIVE.release()
        return JSONResponse(status_code=502,
                            content=as_openai_error(e.code, str(e)))
    _ACTIVE.release()

    usage = {
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(reply) // 4),
        "total_tokens": (len(prompt) + len(reply)) // 4,
    }
    if decision.kind == "tool":
        message = {"role": "assistant", "content": None,
                   "tool_calls": decision.as_tool_call()}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": decision.final,
                   "reasoning_content": None}
        finish = "stop"
    return _completion(response_id, served, message, finish, usage)


@app.on_event("startup")
async def _startup():
    Path("C:/hermes").mkdir(parents=True, exist_ok=True)


@app.on_event("shutdown")
async def _shutdown():
    for b in BROWSERS.values():
        try:
            await b.close()
        except Exception:
            pass


if __name__ == "__main__":
    s = socket.socket()
    try:
        s.bind((CFG["host"], CFG["port"]))
        s.close()
    except OSError:
        raise SystemExit(f"port {CFG['port']} already in use")
    uvicorn.run(app, host=CFG["host"], port=CFG["port"], log_level="info")
