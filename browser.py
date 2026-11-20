"""Browser driver for brain-proxy.

Launches a dedicated Chromium-family browser on its own persistent profile and
turns a web chat into something that behaves like a model endpoint. One
instance per brain. The profile survives between turns, so the user logs in
once and the login sticks.
"""
import asyncio
import hashlib
import math as _math
import os
import random
import random as _random
import re
import subprocess as _subprocess
import time
import uuid
from pathlib import Path

from patchright.async_api import async_playwright

from breaker import Breaker
from errors import BrainError, COOLDOWN, TRANSIENT, classify

AUTH_URLS = ("/auth", "/login", "/signin", "/sign_in", "/sign-in")
CONTINUE_HINTS = (
    "should i continue",
    "want me to continue",
    "shall i proceed",
    "continue with",
    "let me know if you",
    "would you like me to",
)

AGENT_STATUS = ("orchestrating", "thinking", "working", "planning",
                "searching", "analyzing", "running", "starting")
AGENT_ERROR = ("something went wrong", "please try again",
               "error occurred", "failed to generate")
RATE_LIMIT = ("daily usage limit", "reached your daily", "usage limit reached",
              "usage quota", "has been exhausted", "please try again in")

# Site conditions the proxy recognises and acts on by itself.
#
# These are site-agnostic on purpose: an entry describes what a person would
# do, not which site it is. A free web chat fails in a handful of predictable
# ways - it runs out of memory, stops at a per-turn tool-call limit, fills its
# workspace, expires its session - and each has an obvious human remedy. The
# proxy applies the remedy and waits, and only gives up after `max` attempts.
#
# Phrases are matched case-insensitively against the page text, first match
# wins, so the specific conditions must stay above the generic one.
DEFAULT_RECOVERY = (
    {
        "id": "out_of_memory",
        "phrases": ("ran out of memory", "out of memory",
                    "context length exceeded", "context limit exceeded",
                    "input is too long", "too many tokens"),
        "action": "reprompt",
        "max": 2,
        "msg": "the site ran out of memory",
    },
    {
        "id": "tool_call_limit",
        "phrases": ("reached the tool-call limit", "tool call limit",
                    "send a message to continue", "continue where it left off"),
        "action": "continue",
        "text": "Continue",
        "max": 3,
        "msg": "the site stopped at its per-turn tool-call limit",
    },
    {
        "id": "workspace_full",
        "phrases": ("workspace over budget", "files were not saved",
                    "storage limit exceeded", "workspace is full",
                    "disk quota exceeded"),
        "action": "new_session",
        "max": 2,
        "msg": "the site workspace is over its limit",
    },
    {
        "id": "session_expired",
        "phrases": ("session expired", "page needs to be reloaded",
                    "reload the page", "connection lost", "network error"),
        "action": "refresh",
        "max": 2,
        "msg": "the site session needs a reload",
    },
    {
        "id": "generic_error",
        "phrases": ("something went wrong", "error occurred",
                    "failed to generate"),
        "action": "reprompt",
        "max": 2,
        "msg": "the site reported an error",
    },
)


BRAIN_PREAMBLE = (
    "[BRAIN PROXY]\n"
    "You are the reasoning engine inside an agent framework. Your reply is used "
    "verbatim as the assistant's next message. You have no tools, no file access "
    "and no browser. Never create files unless the task explicitly asks for one. "
    "If the framework's tools are listed below, reply with exactly one JSON object "
    "and nothing else: {\"tool\": \"<name>\", \"args\": {...}}. When the task is "
    "finished, reply with {\"final\": \"<answer>\"}. A marker token at the end of "
    "the prompt is machine-generated - ignore it and never repeat it.\n"
)

BRAIN_PREAMBLE_PLAIN = (
    "[BRAIN PROXY]\n"
    "You are the reasoning engine inside an agent framework. Your reply is used "
    "verbatim as the assistant's next message. You have no tools, no file access "
    "and no browser. Never create files unless the task explicitly asks for one. "
    "Answer directly in plain prose - do NOT output JSON, do NOT wrap your reply "
    "in markers. A marker token at the end of the prompt is machine-generated - "
    "ignore it and never repeat it.\n"
)


_SHARED = {"pw": None, "ctx": None}

# Playwright passes --disable-blink-features=AutomationControlled by default.
# Brave prints a warning banner for it on every page and it is a well-known
# automation fingerprint, so it is explicitly dropped from the defaults;
# navigator.webdriver is hidden by the init script at launch instead, which
# leaves nothing behind in the browser's own command line.
IGNORE_DEFAULT_ARGS = ["--disable-blink-features=AutomationControlled"]

# Extra arguments for the browser process. Deliberately empty: every flag
# Playwright does not add itself is a potential automation tell.
LAUNCH_ARGS = []


class BrainBrowser:
    def __init__(self, name, cfg, browser_cfg, recovery=None):
        self.name = name
        self.url = cfg["url"]
        self.input_selectors = cfg.get("input_selectors") or [cfg.get("input_selector", "textarea")]
        self.response_selectors = cfg.get("response_selectors") or []
        self.submit_selectors = cfg.get("submit_selectors") or []
        self.submit_key = cfg.get("submit_key", "Enter")
        self.done_when_gone = cfg.get("done_when_gone") or []
        self.mode = cfg.get("mode", "chat")
        self.agent_deadline = cfg.get("agent_deadline", 900)
        self.min_gap = float(cfg.get("min_gap_seconds", 3))
        self.last_send = 0.0
        self.auth_urls = cfg.get("auth_urls", [])
        self.auth_selectors = cfg.get("auth_selectors", [])
        self.strip_patterns = cfg.get("strip_patterns") or []
        self.paused = False
        self._human = None
        self._last_activity = time.time()
        self._idle_task = None
        self.file_input_selectors = cfg.get("file_input_selectors") or ["input[type='file']"]

        # Site-condition recovery: the built-in defaults plus anything the
        # config adds, so a new site can be taught without touching the code.
        extra = recovery or cfg.get("recovery") or []
        self.conditions = tuple(DEFAULT_RECOVERY) + tuple(extra)
        self._cond_counts = {}
        self._cond_seen = {}
        self._run_url = ""
        self._run_key = None
        # Seconds to wait, per attempt, when the human is typing in the
        # composer. Configurable so tests do not actually sleep.
        self.busy_wait_base = 20.0

        self.user_data_dir = browser_cfg["user_data_dir"]
        self.executable_path = browser_cfg.get("executable_path") or ""
        self.browser_cfg = browser_cfg or {}
        self._context = None
        self._pw = None

        self._browser = None
        self._page = None
        self.ready = False
        self._lock = asyncio.Lock()
        self.session_url = ""
        self.learned_input_selectors = []
        self.learned_response_selectors = []
        self.sessions = {}
        self.breaker = Breaker()
        self.turn_index = 0
        self.max_turns = 40
        self.reanchor_every = 10
        # Snapshot of the response bubbles as they were *before* we sent, so
        # _extract() can tell a new reply from the previous turn's answer.
        self._pre_bubbles = None
        # The last prompt we typed; used to recognise our own leftovers in the
        # composer and to detect an answer that merely echoes our prompt.
        self._last_typed = ""
        # The marker token belonging to that last prompt.
        self._prev_marker = ""

        try:
            import json as _j, pathlib as _p
            f = _p.Path("C:/hermes/brain-proxy/learned.json")
            if f.exists():
                data = _j.loads(f.read_text(encoding="utf-8")).get(self.name, {})
                self.session_url = data.get("session_url", "")
                for s in data.get("input", [])[::-1]:
                    if s not in self.input_selectors:
                        self.input_selectors.insert(0, s)
                for s in data.get("response", [])[::-1]:
                    if s not in self.response_selectors:
                        self.response_selectors.insert(0, s)
                self.learned_input_selectors = list(data.get("input", []))
                self.learned_response_selectors = list(data.get("response", []))
                self.sessions = dict(data.get("sessions") or {})
        except Exception:
            pass

    # ---------- lifecycle ----------

    def _place_window(self):
        """Put the OS window somewhere sensible, once, at launch.

        The page itself is fluid (no_viewport), so this is only a starting
        position: the user can move and resize the window freely afterwards.
        """
        try:
            import ctypes
            import subprocess

            user32 = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            work = RECT()
            user32.SystemParametersInfoA(0x0030, 0, ctypes.byref(work), 0)
            screen_w = work.right - work.left
            screen_h = work.bottom - work.top
            if screen_w <= 0 or screen_h <= 0:
                return

            win_w = min(1280, screen_w)
            win_h = min(760, screen_h)
            x = max(0, (screen_w - win_w) // 2)
            y = max(0, screen_h - win_h)

            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='brave.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*brain-proxy*profile*' } | "
                 "Select-Object -First 1 -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=20).stdout.strip()
            pid = None
            for line in out.splitlines():
                if line.strip().isdigit():
                    pid = int(line.strip())
                    break
            if not pid:
                return
            out2 = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process -Id %d -ErrorAction SilentlyContinue).MainWindowHandle"
                 % pid],
                capture_output=True, text=True, timeout=20).stdout.strip()
            if not out2.strip().isdigit():
                return
            hwnd = int(out2.strip())
            user32.MoveWindow(hwnd, x, y, win_w, win_h, True)
            print(f"[brain-proxy] window placed at {x},{y} ({win_w}x{win_h})",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[brain-proxy] place failed: {e}", flush=True)

    async def _page_alive(self, page, timeout=4.0):
        try:
            await asyncio.wait_for(page.evaluate("() => 1"), timeout=timeout)
            return not page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _same_url(a, b):
        if not a or not b:
            return False
        return a.rstrip("/").split("?")[0] == b.rstrip("/").split("?")[0]

    async def _context_alive(self):
        """Is the shared browser process still running?

        The context object outlives the process: after the browser is closed
        (by the user, by a crash, or by a kill) the handle is still non-None
        but every call on it fails with TargetClosedError. That must read as
        dead, or the proxy reuses a corpse forever and never relaunches.

        is_connected() can flap once under load, so a single False is confirmed
        before declaring the browser dead - dropping a healthy context would
        kill the window the user is looking at.
        """
        ctx = self._context
        if ctx is None:
            return False
        for attempt in range(2):
            try:
                browser = ctx.browser
                if browser is not None and browser.is_connected():
                    return True
            except Exception:
                pass
            if attempt == 0:
                time.sleep(1.0)
        return False

    async def browser_needs_relaunch(self):
        """True only when a browser existed and its process has since died.

        Distinct from "never launched": an open circuit must not be reset for a
        brain that simply has not been used yet.
        """
        if self._context is None:
            return False
        return not await self._context_alive()

    def _kill_profile_holders(self):
        """Kill any Chromium still holding our profile.

        A hard kill, or a browser still shutting down, leaves the profile
        locked; the next launch then fails with "Opening in existing browser
        session" and nothing recovers. Only processes whose command line names
        THIS profile are touched.
        """
        try:
            ps = ("Get-CimInstance Win32_Process -Filter \"name='brave.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*" + self.user_data_dir +
                  "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
            _subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            print(f"[brain-proxy] {self.name}: could not clear profile holders: "
                  f"{type(e).__name__}: {e}", flush=True)

    async def _launch_browser(self):
        """Launch the persistent context, tolerating a locked profile.

        Order: with the sandbox, then without it (a machine that cannot
        sandbox must still work). A locked profile is cleared and retried.
        """
        sandbox = bool(self.browser_cfg.get("sandbox", True))
        options = [True, False] if sandbox else [False]
        last = None
        for sb in options:
            for _ in range(2):
                try:
                    return await self._pw.chromium.launch_persistent_context(
                        user_data_dir=self.user_data_dir,
                        headless=False,
                        executable_path=self.executable_path or None,
                        no_viewport=True,
                        chromium_sandbox=sb,
                        args=LAUNCH_ARGS,
                        ignore_default_args=IGNORE_DEFAULT_ARGS,
                    )
                except Exception as e:
                    last = e
                    msg = str(e)
                    if ("existing browser session" in msg
                            or "profile is already in use" in msg):
                        print(f"[brain-proxy] {self.name}: profile is locked, "
                              f"clearing the holder and retrying", flush=True)
                        self._kill_profile_holders()
                        continue
                    if sb:
                        print(f"[brain-proxy] {self.name}: sandbox unavailable "
                              f"({type(e).__name__}: "
                              f"{msg.splitlines()[0][:160]}), retrying without it",
                              flush=True)
                        break
                    raise
        raise last

    async def _drop_dead_context(self):
        """Forget a dead browser so the next connect launches a fresh one."""
        ctx, self._context = self._context, None
        self._pw = None
        self.ready = False
        self._page = None
        if _SHARED.get("ctx") is not None:
            _SHARED["ctx"] = None
            _SHARED["pw"] = None
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    async def _connect(self, url=None):
        """Attach to this brain's page, optionally on a specific thread.

        `url` is used to resume the conversation that belongs to the current
        task, instead of whichever thread happened to be open last.
        """
        if url:
            self.session_url = url

        if self.ready and self._page is not None:
            if await self._page_alive(self._page):
                if not url or self._same_url(self._page.url, url):
                    return
                try:
                    await self._page.goto(url, wait_until="domcontentloaded")
                    self.session_url = self._page.url
                    await self._page.bring_to_front()
                    return
                except Exception:
                    self.ready = False
            else:
                self.ready = False

        base = self.url.split("?")[0].rstrip("/")
        if self.session_url and not self.session_url.startswith(base):
            self.session_url = ""

        # The browser may have been closed since we last used it. Dropping the
        # stale handle here is what lets the proxy recover on its own.
        if self._context is not None and not await self._context_alive():
            print(f"[brain-proxy] {self.name}: browser process is gone, "
                  f"relaunching", flush=True)
            await self._drop_dead_context()

        if self._context is None:
            if _SHARED["ctx"] is not None:
                # one browser for every brain: a second persistent context on
                # the same profile would fail with "existing browser session"
                self._context = _SHARED["ctx"]
                self._pw = _SHARED["pw"]
            else:
                self._pw = await async_playwright().start()
                self._context = await self._launch_browser()
                await self._context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                _SHARED["ctx"] = self._context
                _SHARED["pw"] = self._pw
                self._place_window()

        ctx = self._context
        if self.session_url:
            for p in ctx.pages:
                if self._same_url(p.url, self.session_url) and await self._page_alive(p):
                    self._page = p
                    self.session_url = p.url
                    await p.bring_to_front()
                    self.ready = True
                    print(f"[brain-proxy] {self.name} resumed {p.url}", flush=True)
                    return
            # No tab is on that thread yet - move an existing tab there rather
            # than opening yet another one.
            for p in ctx.pages:
                if p.url.startswith(base) and await self._page_alive(p):
                    try:
                        await p.goto(self.session_url, wait_until="domcontentloaded")
                        self._page = p
                        self.session_url = p.url
                        await p.bring_to_front()
                        self.ready = True
                        print(f"[brain-proxy] {self.name} resumed {self.session_url}",
                              flush=True)
                        return
                    except Exception:
                        break
        for p in ctx.pages:
            if p.url.startswith(base) and await self._page_alive(p):
                self._page = p
                self.session_url = p.url
                await p.bring_to_front()
                self.ready = True
                print(f"[brain-proxy] {self.name} attached {p.url}", flush=True)
                return
        page = await ctx.new_page()
        await page.goto(self.session_url or self.url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        self._page = page
        self.session_url = page.url
        await page.bring_to_front()
        self.ready = True
        print(f"[brain-proxy] {self.name} opened {page.url}", flush=True)

    async def close(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        _SHARED["ctx"] = None
        _SHARED["pw"] = None

    # ---------- helpers ----------

    async def _first(self, selectors, timeout=2000):
        for sel in selectors or []:
            try:
                els = self._page.locator(sel)
                n = await els.count()
                for i in range(n):
                    el = els.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                        el_id = (await el.get_attribute("id")) or ""
                        el_cls = (await el.get_attribute("class")) or ""
                        if "recaptcha" in el_id.lower():
                            continue
                        if re.search(r"(^|\s)hidden(\s|$)", el_cls, re.I):
                            continue
                        return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    async def _body_text(self):
        try:
            return await self._page.evaluate("document.body.innerText")
        except Exception:
            return ""

    async def _preflight(self):
        """Cheap reachability check before touching Playwright."""
        import socket as _s
        from urllib.parse import urlparse
        u = urlparse(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            fam = _s.AF_INET6 if ":" in host else _s.AF_INET
            s = _s.socket(fam, _s.SOCK_STREAM)
            s.settimeout(2.0)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, s.connect, (host, port))
            s.close()
        except Exception as e:
            raise BrainError("upstream_offline",
                             f"{host}:{port} unreachable ({e})", self.name)

    async def _guard_ui(self):
        """Fail fast on walls the proxy must not try to solve."""
        try:
            title = (await self._page.title()) or ""
        except Exception:
            title = ""
        low = title.lower()
        if any(k in low for k in ("just a moment", "cloudflare", "attention required",
                                  "verify you are human", "access denied")):
            raise BrainError("captcha", f"{self.name} hit a challenge page: {title}",
                             self.name)
        hit = await self._find_text(RATE_LIMIT)
        if hit:
            raise BrainError("rate_limited", f"{self.name}: {hit}", self.name)
        try:
            if any(u in self._page.url for u in AUTH_URLS):
                raise BrainError("auth_wall", f"{self.name} is at {self._page.url}",
                                 self.name)
        except BrainError:
            raise
        except Exception:
            pass
        # arena's post-task feedback dialog steals the composer
        try:
            await self._handle_arena_feedback()
        except Exception:
            pass
        # cookie / consent wall: click the plainly-safe buttons once, then
        # surface it if it is still there
        try:
            await self._dismiss_safe_dialogs()
            for sel in ("text=I accept", "text=Accept all", "text=Accept cookies",
                        "text=Agree", "button:has-text(\"Accept\")"):
                if await self._page.locator(sel).count():
                    raise BrainError("consent_wall",
                                     f"{self.name} has a consent dialog", self.name)
        except BrainError:
            raise
        except Exception:
            pass

    async def _continue_generating(self):
        for txt in ("Continue generating", "Keep generating", "Show more"):
            try:
                loc = self._page.locator(f"text={txt}")
                if await loc.count():
                    await loc.first.click(timeout=2000)
                    return True
            except Exception:
                continue
        return False

    def _same_site(self, url):
        base = self.url.split("?")[0].rstrip("/")
        return bool(url) and url.split("?")[0].rstrip("/").startswith(base)

    async def _note_url_change(self, log=None):
        """Re-anchor when the tab moves to another thread on the same site.

        The user can change tabs or threads mid-run, and the site itself can
        navigate. Following the new thread is what a person would do; only
        leaving the site entirely is treated as a failure.

        Returns True when the URL changed and we followed it.
        """
        try:
            cur = self._page.url
        except Exception:
            return False
        if not cur or not self._run_url or cur == self._run_url:
            return False
        if self._same_site(cur):
            msg = f"URL_CHANGE {self._run_url} -> {cur}"
            if log:
                log(msg)
            else:
                print(f"[brain-proxy] {self.name}: {msg}", flush=True)
            self.session_url = cur
            self._run_url = cur
            if self._run_key:
                self.sessions[self._run_key] = cur
            return True
        msg = f"LEFT_SITE {cur}"
        if log:
            log(msg)
        else:
            print(f"[brain-proxy] {self.name}: {msg}", flush=True)
        raise BrainError("site_changed", f"tab navigated away to {cur}", self.name)

    async def _detect_condition(self, before_text=""):
        """Return the first site condition the page shows, or None.

        A phrase only counts when it is *new* since `before_text` - the page as
        it was before we sent. Without that test the brain's own answer would
        trigger recoveries: an assistant explaining "the workspace is over
        budget" is not the site reporting a fault.

        A condition that was already on the page is retried only after a cool
       -off, so a banner that survives one attempt gets one more chance but a
        healthy turn is never re-triggered.
        """
        try:
            body = await self._body_text()
        except Exception:
            return None
        low = body.lower()
        before_low = (before_text or "").lower()
        for cond in self.conditions:
            hit = next((p for p in (cond.get("phrases") or []) if p in low), None)
            if not hit:
                continue
            if hit not in before_low:
                return cond
            last = self._cond_seen.get(cond.get("id"), 0.0)
            if last and (time.time() - last) > 240:
                return cond
        return None

    async def _recover_if_needed(self, before_text=""):
        """Detect a site condition and apply its remedy. True if we acted."""
        cond = await self._detect_condition(before_text)
        if not cond:
            return False
        cid = cond.get("id") or cond.get("action") or "condition"
        n = self._cond_counts.get(cid, 0)
        limit = int(cond.get("max", 2))
        if n >= limit:
            raise BrainError(
                "site_error",
                f"{cid}: still failing after {n} recovery attempt(s)",
                self.name,
            )
        self._cond_counts[cid] = n + 1
        self._cond_seen[cid] = time.time()
        print(f"[brain-proxy] {self.name}: {cid} - "
              f"{cond.get('msg', 'recovering')} (attempt {n + 1}/{limit})",
              flush=True)
        acted = await self._recover(cond)
        if acted:
            await asyncio.sleep(3)
        return acted

    async def _recover(self, cond):
        action = cond.get("action")
        if action == "reprompt":
            return await self._act_reprompt()
        if action == "continue":
            return await self._act_continue(cond.get("text", "Continue"))
        if action == "refresh":
            return await self._act_refresh()
        if action == "new_session":
            return await self._act_new_session()
        if action == "click":
            return await self._act_click(cond.get("buttons") or [])
        return False

    async def _clear_ours(self, el):
        """Clear the composer, but only text we put there."""
        try:
            cur = (await self._composer_text(el)).strip()
        except Exception:
            return
        if cur and not self._composer_text_is_ours(cur):
            # The human is typing. Never destroy their input.
            raise BrainError("user_tab_busy",
                             f"{self.name}: composer has text we did not write",
                             self.name)
        if cur:
            await el.click()
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Delete")
            await asyncio.sleep(0.3)

    async def _act_reprompt(self):
        """The site said 'try again': resend the prompt we already sent."""
        el = await self._composer_ready()
        if el is None:
            return False
        await self._clear_ours(el)
        await self._type_and_send(el, self._last_typed)
        return True

    async def _act_continue(self, text):
        """The site stopped mid-turn: nudge it the way a person would."""
        el = await self._composer_ready()
        if el is None:
            return False
        await self._clear_ours(el)
        await self._type_and_send(el, text)
        return True

    async def _act_refresh(self):
        """Reload the tab and re-check for walls."""
        try:
            await self._page.reload(wait_until="domcontentloaded")
        except Exception:
            return False
        await asyncio.sleep(3)
        try:
            await self._guard_ui()
        except BrainError:
            raise
        except Exception:
            pass
        return True

    async def _act_new_session(self):
        """The thread is unusable (usually full): start a fresh one."""
        await self.new_thread()
        try:
            self.session_url = self._page.url
        except Exception:
            pass
        if self._run_key:
            self.sessions.pop(self._run_key, None)
        self._save_learned()
        return True

    async def _act_click(self, labels):
        for label in labels:
            try:
                loc = self._page.get_by_text(label, exact=False)
                if await loc.count() and await loc.first.is_visible():
                    await loc.first.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    async def _any_present(self, selectors):
        for sel in selectors or []:
            try:
                if await self._page.locator(sel).count():
                    return True
            except Exception:
                continue
        return False

    async def new_thread(self):
        """Start a fresh web chat. Best-effort: navigates back to the base URL."""
        try:
            await self._page.goto(self.url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except Exception:
            pass
        self.turn_index = 0

    async def _type(self, el, text):
        tag = (await el.evaluate("e => e.tagName.toLowerCase()")).lower()
        await el.click()
        if tag in ("textarea", "input"):
            try:
                await el.fill(text)
                return
            except Exception:
                pass
        # contenteditable / React editors: real key events
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
        await self._page.keyboard.insert_text(text)

    async def _submit(self, el):
        if self.submit_key and self.submit_key.lower() != "click":
            await self._page.keyboard.press(self.submit_key)
            await asyncio.sleep(0.6)
            remaining = ""
            try:
                remaining = await el.evaluate("e => (e.innerText || e.value || '').trim()")
            except Exception:
                pass
            if not remaining:
                return
        btn = await self._first(self.submit_selectors, timeout=3000)
        if btn:
            await btn.click()

    async def _composer_ready(self):
        for sel in self.input_selectors:
            loc = self._page.locator(sel)
            for i in range(await loc.count()):
                el = loc.nth(i)
                if not (await el.is_visible() and await el.is_enabled()):
                    continue
                ident = ((await el.get_attribute("id") or "") + " " +
                         (await el.get_attribute("class") or "")).lower()
                if "recaptcha" in ident:
                    continue
                if re.search(r"(^|\s)hidden(\s|$)", ident):
                    continue
                return el
        return None

    async def _handle_arena_feedback(self):
        """Answer Arena's post-task "Was this task successful?" dialog.

        The correct answer is **Keep working**: it closes the dialog and lets
        the agent carry on. "No" offers an undo that can roll back work that
        has already been done, so it is never clicked. If "Keep working" is
        not on the page we fall back to Escape, which dismisses the dialog
        without picking an option at all.
        """
        try:
            if not await self._page.locator(
                "text=Was this task successful?"
            ).count():
                return False
        except Exception:
            return False

        for sel in ("text=Keep working",
                    "button:has-text('Keep working')",
                    "[role='button']:has-text('Keep working')"):
            try:
                loc = self._page.locator(sel)
                if await loc.count():
                    await loc.first.click(timeout=4000)
                    await asyncio.sleep(0.8)
                    print(f"[brain-proxy] {self.name}: arena feedback -> "
                          f"Keep working", flush=True)
                    return True
            except Exception:
                continue

        try:
            await self._page.keyboard.press("Escape")
            await asyncio.sleep(0.6)
            print(f"[brain-proxy] {self.name}: arena feedback dismissed "
                  f"(no Keep working button found)", flush=True)
        except Exception:
            pass
        return False

    async def _dismiss_safe_dialogs(self):
        safe = (
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('Got it')",
            "button:has-text('I agree')",
            "button:has-text('OK')",
        )
        for sel in safe:
            try:
                loc = self._page.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    await loc.first.click(timeout=2000)
                    await asyncio.sleep(0.5)
            except Exception:
                continue

    async def _composer_text(self, el):
        tag = (await el.evaluate("e => e.tagName")).lower()
        if tag in ("textarea", "input"):
            return (await el.input_value()) or ""
        return (await el.inner_text()) or ""

    def _composer_text_is_ours(self, cur):
        """Is the text currently in the composer text that we put there?

        Our own prompts are recognisable by the marker token we append and by
        the distinctive preamble. Anything else was typed by the human, and
        must never be deleted.

        The test has to be tight in both directions. Too tight and the proxy
        refuses to run because it mistakes its own leftover for user input;
        too loose and a two-word message from the human ("ok") gets wiped.
        """
        cur = (cur or "").strip()
        if not cur:
            return True

        prev = (getattr(self, "_last_typed", "") or "").strip()
        prev_marker = getattr(self, "_prev_marker", "") or ""

        # Our previous prompt, unchanged.
        if prev and cur == prev:
            return True
        # Our previous prompt, cut short. If a send was interrupted (killed
        # process, dropped connection) the composer is left holding a prefix
        # of what we typed, and it is ours by construction. A prefix match is
        # strong evidence - the human's message would have to coincidentally
        # be a leading slice of our prompt.
        if prev and cur and len(cur) < len(prev) and prev.startswith(cur):
            return True
        # Our previous prompt, cut short by the site. Only long text counts:
        # a short match is far more likely to be a word the human typed that
        # happens to appear somewhere in our prompt.
        if prev and len(cur) >= 40 and cur in prev:
            return True
        # The marker token is the only recognisable part of a later-turn
        # prompt, because the preamble is only sent on the first turn.
        if prev_marker and prev_marker in cur:
            return True
        # The preamble.
        if cur.startswith("[BRAIN PROXY]"):
            return True
        if "reasoning engine inside an agent framework" in cur.lower():
            return True
        return False

    async def _human_type(self, el, prompt):
        """Type like a person, but bounded.

        A real Hermes turn carries an 8k-char prompt. At 65ms/char that is
        nine minutes of typing, which blows every timeout in the stack. So
        short prompts are typed key-by-key; long ones get a human-typed
        opening and the remainder pasted in chunks - pasting long text is
        itself ordinary human behaviour.
        """
        tag = (await el.evaluate("e => e.tagName")).lower()
        if tag in ("textarea", "input") and len(prompt) > 400:
            await el.fill(prompt)
            return

        # These sites submit on Enter, so a newline in the prompt must go in
        # as Shift+Enter - a bare newline can end the turn early.
        parts = prompt.split("\n")
        for pi, part in enumerate(parts):
            if pi:
                await self._page.keyboard.press("Shift+Enter")
                await asyncio.sleep(_random.uniform(0.15, 0.5))
            if not part:
                continue
            # small chunks, not one character at a time: per-character
            # insert_text races the editor's own re-render and drops or
            # reorders keystrokes, which is what produced the typos
            step = _random.randint(6, 18)
            for i in range(0, len(part), step):
                chunk = part[i:i + step]
                await self._page.keyboard.insert_text(chunk)
                d = _random.gauss(0.07, 0.03)
                d = max(0.03, min(0.16, d))
                if chunk[-1] in " .,;:!?\"'":
                    d += _random.uniform(0.05, 0.22)
                await asyncio.sleep(d)
            if _random.random() < 0.12:
                await asyncio.sleep(_random.uniform(0.3, 1.1))

    async def _type_and_send(self, el, prompt):
        _gap = time.time() - self.last_send
        if _gap < self.min_gap:
            await asyncio.sleep(self.min_gap - _gap + random.uniform(0, 2))
        await self._dismiss_safe_dialogs()

        # reading dwell before touching the composer
        await asyncio.sleep(_random.uniform(0.8, 2.5))

        # approach the composer along a path, overshoot, then correct
        try:
            box = await el.bounding_box()
            if box:
                tx = box["x"] + box["width"] * _random.uniform(0.3, 0.7)
                ty = box["y"] + box["height"] * _random.uniform(0.4, 0.7)
                await self._page.mouse.move(
                    tx + _random.uniform(-80, 80),
                    ty + _random.uniform(-60, 60),
                    steps=_random.randint(8, 18),
                )
                await asyncio.sleep(_random.uniform(0.15, 0.4))
                await self._page.mouse.move(tx, ty, steps=_random.randint(3, 8))
        except Exception:
            pass

        # press with a real hold, not a zero-duration click
        try:
            await self._page.mouse.down()
            await asyncio.sleep(_random.uniform(0.05, 0.18))
            await self._page.mouse.up()
        except Exception:
            await el.click()
        await asyncio.sleep(_random.uniform(0.2, 0.5))

        await self._page.keyboard.press("Control+A")
        await asyncio.sleep(_random.uniform(0.1, 0.3))
        await self._page.keyboard.press("Delete")
        await asyncio.sleep(_random.uniform(0.2, 0.6))

        await self._human_type(el, prompt)

        await asyncio.sleep(0.3)
        for _ in range(8):
            if (await self._composer_text(el)).strip():
                break
            await asyncio.sleep(0.5)
        if not (await self._composer_text(el)).strip():
            raise BrainError("send_failed", "composer empty after typing", self.name)

        # re-read before sending
        await asyncio.sleep(_random.uniform(0.5, 1.8))
        await self._page.keyboard.press(self.submit_key or "Enter")
        for _ in range(6):
            await asyncio.sleep(0.5)
            if not (await self._composer_text(el)).strip():
                self.last_send = time.time()
                self._start_idle_wander()
                return
        btn = await self._first(self.submit_selectors, timeout=1000)
        if btn:
            await btn.click()
            await asyncio.sleep(1)
            if not (await self._composer_text(el)).strip():
                self.last_send = time.time()
                self._start_idle_wander()
                return
        await self._page.evaluate(
            "() => { const f = document.querySelector('form'); if (f) f.requestSubmit(); }"
        )
        await asyncio.sleep(1)
        if (await self._composer_text(el)).strip():
            raise BrainError("send_failed", "could not submit", self.name)

    def _start_idle_wander(self):
        """Background idle movement between turns.

        Drifts the mouse and scrolls a little so the tab does not look like
        a headless puppet between calls. Fire-and-forget, never blocks.
        """
        asyncio.create_task(self._idle_wander())

    async def _idle_wander(self):
        try:
            await asyncio.sleep(_random.uniform(1.0, 3.0))
            w = await self._page.evaluate("() => window.innerWidth")
            h = await self._page.evaluate("() => window.innerHeight")
            for _ in range(_random.randint(1, 3)):
                await self._page.mouse.move(
                    _random.randint(int(w * 0.1), int(w * 0.9)),
                    _random.randint(int(h * 0.1), int(h * 0.9)),
                    steps=_random.randint(5, 15),
                )
                await asyncio.sleep(_random.uniform(0.4, 1.2))
            await self._page.mouse.wheel(
                0, _random.choice([-1, 1]) * _random.randint(80, 300))
        except Exception:
            pass

    def _is_status(self, text):
        t = (text or "").strip().lower()
        return len(t) < 60 and any(w in t for w in AGENT_STATUS)

    async def _find_text(self, needles):
        body = (await self._body_text()).lower()
        for n in needles:
            if n in body:
                return n
        return None

    async def _last_assistant_text(self):
        for sel in self.response_selectors:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                if n:
                    return (await loc.nth(n - 1).inner_text()).strip()
            except Exception:
                continue
        return ""

    async def _wait_agent(self, before_text):
        deadline = time.time() + getattr(self, "agent_deadline", 7200)
        log_dir = Path("C:/hermes/brain-proxy/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{self.name}-{int(time.time())}.log"

        def log(msg):
            try:
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            except Exception:
                pass

        deadline_s = int(getattr(self, "agent_deadline", 7200))
        has_stop = bool(self.done_when_gone)
        log(f"START deadline={deadline_s}s has_stop={has_stop} url={self._page.url}")

        state = "starting"
        last_text = before_text
        last_change = time.time()
        no_stop_since = None if has_stop else time.time()
        check_n = 0

        while time.time() < deadline:
            await asyncio.sleep(5)
            check_n += 1

            if not await self._page_alive(self._page, timeout=4):
                log(f"DEAD tab_unresponsive check={check_n}")
                raise BrainError("tab_dead", "tab unresponsive", self.name)

            try:
                body = await self._body_text()
            except Exception as e:
                log(f"DEAD body_eval {type(e).__name__}: {e}")
                raise BrainError("tab_dead", f"{type(e).__name__}: {e}", self.name)

            # A tab or session can change under us mid-run - the user picks a
            # different thread, or the site navigates.
            await self._note_url_change(log)

            # A recoverable site condition is acted on before the fatal list
            # below: "something went wrong" and friends are often just a turn
            # that needs resending.
            if check_n % 2 == 0:
                try:
                    acted = await self._recover_if_needed(before_text)
                except BrainError:
                    raise
                except Exception:
                    acted = False
                if acted:
                    log(f"RECOVERED check={check_n}")
                    # The remedy changed the page; restart the state machine
                    # so its progress is measured from here, not from before.
                    try:
                        before_text = await self._body_text()
                    except Exception:
                        pass
                    last_text = before_text
                    last_change = time.time()
                    no_stop_since = None if has_stop else time.time()
                    state = "starting"
                    continue

            for phrase in AGENT_ERROR:
                if phrase in body.lower():
                    log(f"FAILED phrase={phrase!r}")
                    raise BrainError("site_error", phrase, self.name)

            # Arena can pop its feedback dialog mid-run and steal the
            # composer. Answer it with "Keep working" so the agent carries
            # on. Deliberately infrequent - polling the page aggressively
            # is what triggers human-verification checks.
            if check_n % 6 == 0:
                try:
                    if await self._handle_arena_feedback():
                        log(f"DIALOG arena feedback answered (keep working) "
                            f"check={check_n}")
                except Exception:
                    pass

            stop = await self._first(self.done_when_gone, timeout=500) if has_stop else None
            changed = body != last_text
            if changed:
                last_text = body
                last_change = time.time()
            elapsed_stable = time.time() - last_change
            has_new = body != before_text

            if state == "starting" and (stop or has_new):
                state = "working"
                log(f"TRANS starting->working stop={bool(stop)} new={has_new}")

            if has_stop:
                if stop:
                    no_stop_since = None
                    if state == "working" and elapsed_stable >= 30:
                        state = "thinking"
                        log(f"TRANS working->thinking stable={int(elapsed_stable)}s")
                    continue
                if no_stop_since is None:
                    no_stop_since = time.time()
                no_stop_elapsed = time.time() - no_stop_since
                if has_new and no_stop_elapsed >= 30 and elapsed_stable >= 15:
                    last = await self._last_assistant_text()
                    if last and not self._is_status(last):
                        log(f"DONE stop_gone={int(no_stop_elapsed)}s stable={int(elapsed_stable)}s")
                        return
                if not has_new and no_stop_elapsed >= 60:
                    log(f"STUCK no_content stop_gone={int(no_stop_elapsed)}s")
                    raise BrainError("no_reply", "stop gone, no content", self.name)
            else:
                if has_new and elapsed_stable >= 15:
                    last = await self._last_assistant_text()
                    if last and not self._is_status(last):
                        log(f"DONE text_stable={int(elapsed_stable)}s")
                        return
                if not has_new and (time.time() - last_change) >= 120:
                    log(f"STUCK no_content stable={int(time.time()-last_change)}s")
                    raise BrainError("no_reply", "no reply after 120s", self.name)

        log(f"TIMEOUT state={state} elapsed={deadline_s}s")
        raise BrainError("agent_timeout", f"agent did not finish in {deadline_s}s", self.name)

    async def _wait_reply(self, before_text):
        if self.mode == "agent":
            deadline = time.time() + 900
            for sel in self.done_when_gone:
                try:
                    el = await self._page.wait_for_selector(sel, timeout=5000)
                    if el:
                        await self._page.wait_for_selector(
                            sel, state="detached",
                            timeout=max(1.0, deadline - time.time()))
                        await asyncio.sleep(3)
                        return
                except Exception:
                    continue
            # no stop control matched: fall back to "text stopped changing"
            last = before_text
            stable = 0
            while time.time() < deadline:
                await asyncio.sleep(2)
                now = await self._body_text()
                if now != before_text and now == last:
                    stable += 1
                    if stable >= 3:
                        return
                else:
                    stable = 0
                    last = now
            return
        # Preferred: a "stop generating" control appears, then disappears.
        # Polled rather than awaited so a tab or thread change mid-turn is
        # noticed too.
        if self.done_when_gone:
            stop_seen = False
            waited = 0.0
            deadline = time.time() + 240
            while time.time() < deadline:
                await asyncio.sleep(1.5)
                waited += 1.5
                await self._note_url_change()
                if await self._any_present(self.done_when_gone):
                    stop_seen = True
                elif stop_seen:
                    return
                elif waited >= 12:
                    # No stop control ever appeared; fall back to "text
                    # stopped changing" below.
                    break
        # Fallback: text stops changing for 4s (and has actually changed).
        last = before_text
        stable = 0
        for _ in range(240):
            await asyncio.sleep(1)
            await self._note_url_change()
            now = await self._body_text()
            if now != before_text and now == last:
                stable += 1
                if stable >= 4:
                    return
            else:
                stable = 0
                last = now

    async def _response_snapshot(self):
        """Capture the current assistant response bubbles.

        Used to prove that a reply we extract actually appeared *after* we
        sent the prompt, rather than being the previous turn's answer still
        sitting on the page.
        """
        texts = []
        for sel in self.response_selectors:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                if n > len(texts):
                    for i in range(n):
                        try:
                            t = (await loc.nth(i).inner_text()).strip()
                        except Exception:
                            t = ""
                        texts.append(t)
                    break
            except Exception:
                continue
        return texts

    async def _extract(self, before_text):
        pre = getattr(self, "_pre_bubbles", None)
        current = await self._response_snapshot()

        # Prefer an explicit response bubble, but only one that is provably
        # new. Returning the last bubble blindly meant a slow brain could be
        # answered with the previous turn's text.
        if current:
            fresh = []
            if pre is None:
                fresh = current
            else:
                if len(current) > len(pre):
                    fresh = current[len(pre):]
                elif current[-1] != pre[-1]:
                    # The last bubble changed in place (streaming / agent mode).
                    fresh = [current[-1]]
            for text in reversed(fresh):
                if text and not self._is_status(text):
                    return self._clean(text)

        after = await self._body_text()
        # Fallback: everything after the marker we appended to the prompt.
        # The marker is unique per send, so this is freshness-safe too.
        marker = getattr(self, "_marker", None)
        if marker:
            idx = after.rfind(marker)
            if idx >= 0:
                tail = after[idx + len(marker):]
                if tail.strip():
                    return self._clean(tail)
        if after.startswith(before_text):
            return self._clean(after[len(before_text):].strip())
        return self._clean(after.strip())

    _CHROME = re.compile(
        r"^\s*(?:[…\.]{1,3}\s*)?(?:Show more\s*)?"
        r"(?:(?:ChatGPT|Gemini|DeepSeek|Arena) said:\s*)+",
        re.IGNORECASE,
    )

    _SAID = re.compile(r"(?:ChatGPT|Gemini|DeepSeek|Arena)\s+said:\s*", re.IGNORECASE)

    _ECHO_PHRASES = (
        "same task",
        "the task is the same",
        "the task is unchanged",
        "task unchanged",
        "no change to the task",
        "continuing with the same task",
        "understood",
        "got it",
        "acknowledged",
    )

    # Deliberately narrow: only an explicit restatement of the JSON contract
    # counts. Anything looser (an answer that merely starts with "I need to
    # reply to...") gets mistaken for an echo and is silently deleted.
    _ECHO_FORMAT_RE = re.compile(
        r"^reply\s+with\s+exactly\s+one\s+json\b", re.IGNORECASE
    )

    _STOPWORDS = frozenset(
        "the a an and or to of in is it you your that this with for on as be "
        "i we my our are was were will would can could should do does did not "
        "but if then than so such have has had".split()
    )

    @staticmethod
    def _norm_ws(s):
        return re.sub(r"\s+", " ", (s or "")).strip().lower()

    def _is_prompt_echo(self, para):
        """True when a paragraph is the brain echoing our own prompt back.

        The old heuristic looked for words like "we need" or "the user wants"
        at the start of the first paragraph and deleted it. That silently ate
        the opening of legitimate answers ("We need to fix the parser...").

        Now the paragraph is only removed when it is provably an echo:
          - it quotes text we actually sent, or
          - nearly all of its content words came from what we sent, or
          - it is an unambiguous "acknowledged" phrase, or
          - it explicitly restates the one-JSON-object contract.
        """
        p = self._norm_ws(para)
        if not p:
            return False

        sent = self._norm_ws(getattr(self, "_last_typed", "") or "")
        if sent:
            # Trailing punctuation often differs (our prompt ends with a
            # colon, the brain's echo with a full stop), so compare without it.
            p_cmp = p.rstrip(".!?;:, ")
            s_cmp = sent.rstrip(".!?;:, ")
            if len(p_cmp) >= 24 and p_cmp in s_cmp:
                return True

            # Word-level check: nearly every content word came from our own
            # prompt. The word count floor keeps ordinary answers out.
            pw = [w for w in re.findall(r"[a-z0-9]+", p) if w not in self._STOPWORDS]
            if len(pw) >= 8:
                sw = set(re.findall(r"[a-z0-9]+", sent))
                if sum(1 for w in pw if w in sw) / len(pw) >= 0.9:
                    return True

        if p.strip(".!?,") in self._ECHO_PHRASES:
            return True

        if len(p) <= 200 and self._ECHO_FORMAT_RE.match(p):
            return True

        return False

    def _clean(self, text):
        """Strip inter-bubble chrome and trailing site boilerplate."""
        text = text.strip()
        said = list(self._SAID.finditer(text))
        if said:
            text = text[said[-1].end():]
        text = re.sub(r"^\s*(?:[…\.]{1,3}\s*)?(?:Show more\s*)?", "", text).strip()
        # arena appends a timestamp + UI labels after the reply
        m = re.search(r"\d{1,2}:\d{2}\s*[AP]M", text)
        if m:
            text = text[: m.start()].strip()
        footers = (
            "Recovering the previous message.",
            "Is this conversation helpful so far?",
            "ChatGPT can make mistakes. Check important info.",
            "ChatGPT is AI and can make mistakes.",
            "Gemini is AI and can make mistakes.",
            "Get responses tailored to you",
            "Log in so ChatGPT can remember what matters to you.",
            "Log in to get answers based on saved chats",
            "Latest response",
            "Response complete",
        )
        for pat in self.strip_patterns:
            text = re.sub(pat, "", text, flags=re.I | re.M).strip()

        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paras) >= 2 and self._is_prompt_echo(paras[0]):
            paras = paras[1:]
            text = "\n\n".join(paras)

        cut = len(text)
        for f in footers:
            i = text.find(f)
            if i >= 0:
                cut = min(cut, i)
        return text[:cut].strip()

    def _trim_footer(self, text):
        return self._clean(text)

    async def _heal(self):
        try:
            data = await self._page.evaluate("""
                () => {
                  const vis = e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 20 && r.height > 10;
                  };
                  const inputs = [...document.querySelectorAll(
                    'textarea, [contenteditable="true"], .ProseMirror, .tiptap'
                  )].filter(e => vis(e) && !(e.id||'').toLowerCase().includes('recaptcha'));
                  const bubbles = [...document.querySelectorAll(
                    'article, [data-message-role], [class*="message"], [class*="markdown"], .prose'
                  )].filter(e => vis(e) && !e.classList.contains('tiptap'));
                  return {
                    inputs: inputs.map(e => ({
                      tag: e.tagName,
                      cls: e.className,
                      id: e.id
                    })),
                    bubbles: bubbles.slice(-5).map(e => ({
                      tag: e.tagName,
                      cls: (typeof e.className === 'string') ? e.className : ''
                    }))
                  };
                }
            """)
        except Exception:
            return False
        added = False
        for i in data.get("inputs", []):
            cls = i.get("cls") or ""
            if "ProseMirror" in cls or "tiptap" in cls:
                sel = "." + ".".join(c for c in cls.split() if c and not c.startswith("["))[:200]
                if sel and sel not in self.input_selectors and sel not in self.learned_input_selectors:
                    self.learned_input_selectors.append(sel)
                    self.input_selectors.insert(0, sel)
                    added = True
        for b in data.get("bubbles", []):
            cls = b.get("cls") or ""
            if "markdown" in cls or "prose" in cls:
                sel = "." + ".".join(c for c in cls.split() if c and not c.startswith("["))[:200]
                if sel and sel not in self.response_selectors and sel not in self.learned_response_selectors:
                    self.learned_response_selectors.append(sel)
                    self.response_selectors.insert(0, sel)
                    added = True
        return added

    def _save_learned(self):
        try:
            import json as _j, pathlib as _p
            out = _p.Path("C:/hermes/brain-proxy/learned.json")
            data = {}
            if out.exists():
                data = _j.loads(out.read_text(encoding="utf-8"))
            data[self.name] = {
                "session_url": self.session_url,
                "input": self.learned_input_selectors,
                "response": self.learned_response_selectors,
                # The per-task -> thread map used to live only in memory, so
                # every restart forgot which thread belonged to which task.
                "sessions": self.sessions,
            }
            out.write_text(_j.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------- public ----------

    async def _attach(self, files):
        """Upload files through the composer's hidden file input."""
        for sel in self.file_input_selectors or []:
            try:
                loc = self._page.locator(sel)
                if await loc.count():
                    await loc.first.set_input_files(files)
                    await asyncio.sleep(1.5)
                    return True
            except Exception:
                continue
        return False

    def _task_key(self, prompt):
        return hashlib.sha256(prompt[:400].encode("utf-8", "ignore")).hexdigest()[:16]

    async def ask(self, prompt, files=None, task_id=None):
        """Send one prompt to the web chat and return its reply.

        `task_id` identifies the *task*, not the turn. It is what lets the
        brain be resumed on the thread that belongs to this task instead of
        whichever thread was open last.
        """
        if self.paused:
            raise BrainError("paused", f"{self.name} is paused by user", self.name)
        last = None
        busy_waits = 0
        for attempt in range(4):
            try:
                reply = await self._ask_once(prompt, files, task_id)
                self.breaker.ok()
                return reply
            except BrainError as e:
                last = e
                if e.code == "user_tab_busy" and busy_waits < 3:
                    # The human is typing in the composer. A person would wait
                    # for them to finish rather than fail the turn - and would
                    # never trip the circuit breaker over it.
                    busy_waits += 1
                    wait = self.busy_wait_base * busy_waits
                    print(f"[brain-proxy] {self.name}: composer in use, waiting "
                          f"{wait}s for the human ({busy_waits}/3)", flush=True)
                    await asyncio.sleep(wait)
                    continue
                self.breaker.fail(e.code, COOLDOWN.get(e.code))
                if e.code in TRANSIENT and attempt < 3:
                    print(f"[brain-proxy] {self.name} [{e.code}] retrying: {e}", flush=True)
                    self.session_url = ""
                    self.ready = False
                    self._page = None
                    await asyncio.sleep(1.0 + random.random())
                    continue
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                code = classify(e)
                self.breaker.fail(code, COOLDOWN.get(code))
                if code in TRANSIENT and attempt < 3:
                    print(f"[brain-proxy] {self.name} [{code}] retrying: {e}", flush=True)
                    self.session_url = ""
                    self.ready = False
                    self._page = None
                    await asyncio.sleep(1.0 + random.random())
                    continue
                raise BrainError(code, f"{type(e).__name__}: {e}", self.name)
        raise BrainError(classify(last) if last else "no_reply", str(last), self.name)

    async def _ask_once(self, prompt, files=None, task_id=None):
        async with self._lock:
            try:
                await self._preflight()
                await self._connect()
            except BrainError:
                raise
            except Exception as e:  # noqa: BLE001
                raise BrainError(classify(e), f"{type(e).__name__}: {e}", self.name)

            await self._guard_ui()
            before = await self._body_text()

            # per-task thread: same task keeps its conversation, a new task
            # gets its own instead of inheriting someone else's.
            #
            # The lookup used to run only while self.session_url was empty.
            # Since session_url is set after every successful turn, the
            # mapping was never consulted again and every new task silently
            # continued in the previous task's thread.
            key = task_id or self._task_key(prompt)
            wanted = self.sessions.get(key, "")
            if wanted and wanted != self.session_url:
                try:
                    await self._connect(url=wanted)
                    await self._guard_ui()
                    before = await self._body_text()
                except BrainError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise BrainError(classify(e), str(e), self.name)

            # Snapshot the response bubbles so we can later prove that the
            # reply we extract is new and not the previous turn's answer.
            self._pre_bubbles = await self._response_snapshot()

            self._marker = f"BR{uuid.uuid4().hex[:8].upper()}"
            note = ""
            if files:
                note = ("\n\n[attachments uploaded: "
                        + ", ".join(os.path.basename(f) for f in files) + "]")

            el = await self._composer_ready()
            if el is None:
                raise BrainError("no_reply", f"no composer on {self._page.url}", self.name)
            cur = (await self._composer_text(el)).strip()
            if cur:
                if not self._composer_text_is_ours(cur):
                    # Anything in the composer that we did not put there is the
                    # human typing into the dedicated browser. The old code
                    # only objected to text of 20+ characters, so a short
                    # message ("stop", "hi") was silently deleted and
                    # overwritten. Never destroy input we did not write.
                    raise BrainError("user_tab_busy",
                                     f"{self.name}: composer already has text "
                                     f"({len(cur)} chars) - refusing to overwrite",
                                     self.name)
                # our own leftover - clear it and continue
                await el.click()
                await self._page.keyboard.press("Control+A")
                await self._page.keyboard.press("Delete")
                await asyncio.sleep(0.3)
            try:
                self._last_typed = prompt + note + "\n\n" + self._marker
                self._prev_marker = self._marker
                self._run_url = self._page.url
                self._run_key = key
                await self._type_and_send(el, self._last_typed)
                if files:
                    await self._attach(files)
            except BrainError:
                raise
            except Exception as e:  # noqa: BLE001
                raise BrainError("send_failed", f"{type(e).__name__}: {e}", self.name)

            for u in self.auth_urls:
                if u in self._page.url:
                    raise BrainError("auth_wall", f"login needed: {self._page.url}",
                                     self.name)
            for sel in self.auth_selectors:
                if await self._page.locator(sel).count():
                    raise BrainError("auth_wall", f"login needed: {self._page.url}",
                                     self.name)

            # A condition can be on the page the moment the turn starts (the
            # previous turn ended on an error banner). Act before waiting.
            try:
                await self._recover_if_needed(before)
            except BrainError:
                raise
            except Exception:
                pass

            try:
                if self.mode == "agent":
                    await self._wait_agent(before)
                else:
                    await self._wait_reply(before)
            except BrainError:
                raise
            except Exception as e:  # noqa: BLE001
                raise BrainError(classify(e), f"{type(e).__name__}: {e}", self.name)

            # ... or it can appear only once the reply has been rendered.
            try:
                if await self._recover_if_needed(before):
                    if self.mode == "agent":
                        await self._wait_agent(before)
                    else:
                        await self._wait_reply(before)
            except BrainError:
                raise
            except Exception:
                pass

            reply = await self._extract(before)
            self.session_url = self._page.url
            self.sessions[key] = self.session_url
            self._save_learned()

            if not reply or len(reply.strip()) < 2:
                if await self._heal():
                    try:
                        await self._submit(el)
                        await self._wait_reply(before)
                        reply = await self._extract(before)
                        self.session_url = self._page.url
                        self.sessions[key] = self.session_url
                        self._save_learned()
                    except Exception:  # noqa: BLE001
                        pass

            if not reply or len(reply.strip()) < 2:
                await asyncio.sleep(3)
                reply = await self._extract(before)

            if reply and await self._continue_generating():
                await self._wait_reply(before)
                reply = await self._extract(before)

            hit = await self._find_text(RATE_LIMIT)
            if hit:
                raise BrainError("rate_limited", f"{self.name}: {hit}", self.name)

            if len(reply) < 2:
                raise BrainError("no_reply", f"{self.name} returned an empty reply",
                                 self.name)

            self.turn_index += 1
            return reply

    async def rotate(self, task_id=None):
        """Ask for a summary, start a fresh chat, seed it, reset the counter.

        The task id is carried through so the rotation does not leave stray
        entries in the per-task thread map.
        """
        summary = await self.ask(
            "Summarize this task so far in under 500 words: the original goal, "
            "decisions made, current state, and what remains. No preamble.",
            task_id=task_id,
        )
        await self.new_thread()
        self.turn_index = 0
        seed = (
            "[CONTEXT HANDOFF]\n"
            + summary
            + "\n\nContinue the same task from where it left off."
        )
        return await self.ask(seed, task_id=task_id)

    def wants_rotation(self):
        return self.turn_index >= self.max_turns
