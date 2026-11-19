"""Browser driver for brain-proxy.

Attaches to a dedicated Chromium-family browser over CDP and turns a web chat
into something that behaves like a model endpoint. One instance per brain.
"""
import asyncio
import os
import re
import time
import uuid
from pathlib import Path

from playwright.async_api import async_playwright

AUTH_URLS = ("/auth", "/login", "/signin", "/sign_in", "/sign-in")
CONTINUE_HINTS = (
    "should i continue",
    "want me to continue",
    "shall i proceed",
    "continue with",
    "let me know if you",
    "would you like me to",
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


class BrainError(RuntimeError):
    pass


class BrainBrowser:
    def __init__(self, name, cfg, browser_cfg):
        self.name = name
        self.url = cfg["url"]
        self.input_selectors = cfg.get("input_selectors") or [cfg.get("input_selector", "textarea")]
        self.response_selectors = cfg.get("response_selectors") or []
        self.submit_selectors = cfg.get("submit_selectors") or []
        self.submit_key = cfg.get("submit_key", "Enter")
        self.done_when_gone = cfg.get("done_when_gone") or []
        self.file_input_selectors = cfg.get("file_input_selectors") or ["input[type='file']"]

        self.cdp_url = browser_cfg["cdp_url"]
        self.executable_path = browser_cfg.get("executable_path") or ""
        self.headless = bool(browser_cfg.get("headless", False))

        self._pw = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()
        self.turn_index = 0
        self.max_turns = 40
        self.reanchor_every = 10

    # ---------- lifecycle ----------

    async def _connect(self):
        if self._page is not None and not self._page.is_closed():
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        )
        page = None
        base = self.url.split("?")[0].rstrip("/")
        for p in ctx.pages:
            if p.url.startswith(base):
                page = p
                break
        if page is None:
            page = await ctx.new_page()
            await page.goto(self.url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        self._page = page
        print(f"[brain-proxy] {self.name} using {page.url}", flush=True)
        await page.bring_to_front()

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

    async def _check_auth(self):
        url = self._page.url
        if any(u in url for u in AUTH_URLS):
            raise BrainError(f"AUTH_REQUIRED: {self.name} is at {url}")

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

    async def _wait_reply(self, before_text):
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
        cut = len(text)
        for f in footers:
            i = text.find(f)
            if i >= 0:
                cut = min(cut, i)
        return text[:cut].strip()

    def _trim_footer(self, text):
        return self._clean(text)

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

    async def ask(self, prompt, files=None):
        """Send one prompt to the web chat and return its reply."""
        async with self._lock:
            await self._connect()
            await self._check_auth()
            before = await self._body_text()

            el = await self._first(self.input_selectors, timeout=30000)
            if el is None:
                raise BrainError(f"NO_INPUT: no matching input on {self._page.url}")

            self._marker = f"BR{uuid.uuid4().hex[:8].upper()}"
            note = ""
            if files:
                note = ("\n\n[attachments uploaded: "
                        + ", ".join(os.path.basename(f) for f in files) + "]")
            await self._type(el, prompt + note + "\n\n" + self._marker)
            if files:
                await self._attach(files)
            await self._submit(el)
            await self._wait_reply(before)
            reply = await self._extract(before)

            if len(reply) < 2:
                await asyncio.sleep(3)
                reply = await self._extract(before)

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
