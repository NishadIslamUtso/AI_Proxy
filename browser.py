"""Browser driver for brain-proxy.

Attaches to a dedicated Chromium-family browser over CDP and turns a web chat
into something that behaves like a model endpoint. One instance per brain.
"""
import asyncio
import hashlib
import os
import random
import re
import time
import uuid
from pathlib import Path

from playwright.async_api import async_playwright

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


class BrainBrowser:
    def __init__(self, name, cfg, browser_cfg):
        self.name = name
        self.url = cfg["url"]
        self.input_selectors = cfg.get("input_selectors") or [cfg.get("input_selector", "textarea")]
        self.response_selectors = cfg.get("response_selectors") or []
        self.submit_selectors = cfg.get("submit_selectors") or []
        self.submit_key = cfg.get("submit_key", "Enter")
        self.done_when_gone = cfg.get("done_when_gone") or []
        self.mode = cfg.get("mode", "chat")
        self.agent_deadline = cfg.get("agent_deadline", 900)
        self.auth_urls = cfg.get("auth_urls", [])
        self.auth_selectors = cfg.get("auth_selectors", [])
        self.strip_patterns = cfg.get("strip_patterns") or []
        self.file_input_selectors = cfg.get("file_input_selectors") or ["input[type='file']"]

        self.cdp_url = browser_cfg["cdp_url"]
        self.executable_path = browser_cfg.get("executable_path") or ""
        self.headless = bool(browser_cfg.get("headless", False))

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
        except Exception:
            pass

    # ---------- lifecycle ----------

    async def _page_alive(self, page, timeout=4.0):
        try:
            await asyncio.wait_for(page.evaluate("() => 1"), timeout=timeout)
            return not page.is_closed()
        except Exception:
            return False

    async def _connect(self):
        if self.ready and self._page is not None:
            if await self._page_alive(self._page):
                return
            self.ready = False

        if self._browser is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
            ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            )
        ctx = self._browser.contexts[0]
        base = self.url.split("?")[0].rstrip("/")

        # 1. remembered session tab, if alive
        if self.session_url:
            for p in ctx.pages:
                if p.url == self.session_url and await self._page_alive(p):
                    self._page = p
                    await p.bring_to_front()
                    self.ready = True
                    return

        # 2. any live tab on the same origin
        for p in ctx.pages:
            if p.url.startswith(base) and await self._page_alive(p):
                self._page = p
                self.session_url = p.url
                await p.bring_to_front()
                self.ready = True
                print(f"[brain-proxy] {self.name} attached to live tab {p.url}", flush=True)
                return

        # 3. open a fresh tab
        page = await ctx.new_page()
        await page.goto(self.url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        self._page = page
        self.session_url = page.url
        await page.bring_to_front()
        self.ready = True
        print(f"[brain-proxy] {self.name} opened new tab {page.url}", flush=True)

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._browser = None
        self._pw = None

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
                        if "recaptcha" in el_cls.lower():
                            continue
                        if "hidden" in el_cls.lower():
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
            if await self._page.locator("text=Was this task successful?").count():
                await self._page.keyboard.press("Escape")
                await asyncio.sleep(0.6)
        except Exception:
            pass
        # cookie / consent wall: never auto-accept, surface it instead
        try:
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
                ident = ((await el.get_attribute("id") or "") +
                         (await el.get_attribute("class") or "")).lower()
                if "recaptcha" in ident or "hidden" in ident:
                    continue
                return el
        return None

    async def _composer_text(self, el):
        tag = (await el.evaluate("e => e.tagName")).lower()
        if tag in ("textarea", "input"):
            return (await el.input_value()) or ""
        return (await el.inner_text()) or ""

    async def _type_and_send(self, el, prompt):
        await el.click()
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
        tag = (await el.evaluate("e => e.tagName")).lower()
        if tag in ("textarea", "input"):
            await el.fill(prompt)
        else:
            await self._page.keyboard.insert_text(prompt)
        await asyncio.sleep(0.3)
        if not (await self._composer_text(el)).strip():
            raise BrainError("send_failed", "composer empty after typing", self.name)
        await self._page.keyboard.press(self.submit_key or "Enter")
        for _ in range(6):
            await asyncio.sleep(0.5)
            if not (await self._composer_text(el)).strip():
                return
        btn = await self._first(self.submit_selectors, timeout=1000)
        if btn:
            await btn.click()
            await asyncio.sleep(1)
            if not (await self._composer_text(el)).strip():
                return
        await self._page.evaluate(
            "() => { const f = document.querySelector('form'); if (f) f.requestSubmit(); }"
        )
        await asyncio.sleep(1)
        if (await self._composer_text(el)).strip():
            raise BrainError("send_failed", "could not submit", self.name)

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
        deadline = time.time() + getattr(self, "agent_deadline", 900)
        last_text, stable = before_text, 0
        while time.time() < deadline:
            await asyncio.sleep(2)
            err = await self._find_text(AGENT_ERROR)
            if err:
                raise BrainError("site_error", err, self.name)
            stop = await self._first(self.done_when_gone, timeout=500)
            now = await self._body_text()
            if now == last_text:
                stable += 1
            else:
                stable, last_text = 0, now
            if stop is None and stable >= 2:
                last = await self._last_assistant_text()
                if last and not self._is_status(last):
                    return
            if stable >= 5 and not self._is_status(await self._last_assistant_text()):
                return
        raise BrainError("agent_timeout", "agent did not finish", self.name)

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
        for sel in self.done_when_gone:
            try:
                await self._page.wait_for_selector(sel, timeout=4000)
                await self._page.wait_for_selector(sel, state="detached", timeout=240000)
                return
            except Exception:
                continue
        # Fallback: text stops changing for 4s (and has actually changed).
        last = before_text
        stable = 0
        for _ in range(240):
            await asyncio.sleep(1)
            now = await self._body_text()
            if now != before_text and now == last:
                stable += 1
                if stable >= 4:
                    return
            else:
                stable = 0
                last = now

    async def _extract(self, before_text):
        if self.mode == "agent":
            for sel in self.response_selectors:
                try:
                    loc = self._page.locator(sel)
                    n = await loc.count()
                    for i in range(n - 1, -1, -1):
                        text = (await loc.nth(i).inner_text()).strip()
                        if text and not self._is_status(text):
                            return self._clean(text)
                except Exception:
                    continue
            return ""
        after = await self._body_text()
        # Preferred: everything after the marker we appended to the prompt.
        marker = getattr(self, "_marker", None)
        if marker:
            idx = after.rfind(marker)
            if idx >= 0:
                tail = after[idx + len(marker):]
                return self._clean(tail)
        for sel in self.response_selectors:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                if n:
                    return self._clean((await loc.nth(n - 1).inner_text()).strip())
            except Exception:
                continue
        if after.startswith(before_text):
            return self._clean(after[len(before_text):].strip())
        return self._clean(after.strip())

    _CHROME = re.compile(
        r"^\s*(?:[…\.]{1,3}\s*)?(?:Show more\s*)?"
        r"(?:(?:ChatGPT|Gemini|DeepSeek|Arena) said:\s*)+",
        re.IGNORECASE,
    )

    _SAID = re.compile(r"(?:ChatGPT|Gemini|DeepSeek|Arena)\s+said:\s*", re.IGNORECASE)

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
        if len(paras) >= 2:
            opener = paras[0].lower()
            if re.match(r"^(we need|the user (wants|asked|is)|i should|i need|okay,? |alright,? |let me)", opener):
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

    async def ask(self, prompt, files=None):
        """Send one prompt to the web chat and return its reply."""
        last = None
        for attempt in range(2):
            try:
                reply = await self._ask_once(prompt, files)
                self.breaker.ok()
                return reply
            except BrainError as e:
                last = e
                self.breaker.fail(e.code, COOLDOWN.get(e.code))
                if e.code in TRANSIENT and attempt == 0:
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
                if code in TRANSIENT and attempt == 0:
                    print(f"[brain-proxy] {self.name} [{code}] retrying: {e}", flush=True)
                    self.session_url = ""
                    self.ready = False
                    self._page = None
                    await asyncio.sleep(1.0 + random.random())
                    continue
                raise BrainError(code, f"{type(e).__name__}: {e}", self.name)
        raise BrainError(classify(last) if last else "no_reply", str(last), self.name)

    async def _ask_once(self, prompt, files=None):
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
            key = self._task_key(prompt)
            if not self.session_url:
                self.session_url = self.sessions.get(key, "")
                if self.session_url:
                    try:
                        await self._connect()
                        await self._guard_ui()
                        before = await self._body_text()
                    except BrainError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        raise BrainError(classify(e), str(e), self.name)

            self._marker = f"BR{uuid.uuid4().hex[:8].upper()}"
            note = ""
            if files:
                note = ("\n\n[attachments uploaded: "
                        + ", ".join(os.path.basename(f) for f in files) + "]")

            el = await self._composer_ready()
            if el is None:
                raise BrainError("no_reply", f"no composer on {self._page.url}", self.name)
            if (await self._composer_text(el)).strip():
                raise BrainError("user_tab_busy",
                                 f"{self.name}: composer already has text", self.name)
            try:
                await self._type_and_send(el, prompt + note + "\n\n" + self._marker)
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

            try:
                if self.mode == "agent":
                    await self._wait_agent(before)
                else:
                    await self._wait_reply(before)
            except BrainError:
                raise
            except Exception as e:  # noqa: BLE001
                raise BrainError(classify(e), f"{type(e).__name__}: {e}", self.name)

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

            if len(reply) < 2:
                raise BrainError("no_reply", f"{self.name} returned an empty reply",
                                 self.name)

            self.turn_index += 1
            return reply

    async def rotate(self):
        """Ask for a summary, start a fresh chat, seed it, reset the counter."""
        summary = await self.ask(
            "Summarize this task so far in under 500 words: the original goal, "
            "decisions made, current state, and what remains. No preamble."
        )
        await self.new_thread()
        self.turn_index = 0
        seed = (
            "[CONTEXT HANDOFF]\n"
            + summary
            + "\n\nContinue the same task from where it left off."
        )
        return await self.ask(seed)

    def wants_rotation(self):
        return self.turn_index >= self.max_turns
