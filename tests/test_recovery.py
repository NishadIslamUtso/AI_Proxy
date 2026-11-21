"""Tests for the site-condition recovery layer.

The phrases here are the exact messages the real sites produced, so a redesign
that stops recognising one of them fails in CI rather than in a live run.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser import DEFAULT_RECOVERY, BrainBrowser  # noqa: E402

CFG = {
    "url": "https://arena.ai/agent",
    "mode": "agent",
    "input_selectors": ["textarea"],
    "response_selectors": ["[data-msg]"],
    "submit_key": "Enter",
}
BROWSER_CFG = {"user_data_dir": "/tmp/bp-recovery-test"}

# The messages that actually appeared on screen, transcribed from screenshots.
REAL_MESSAGES = {
    "out_of_memory": (
        "The assistant ran out of memory. Please try again - this often "
        "resolves on retry."
    ),
    "workspace_full": (
        "Workspace over budget\n\nThis session created 558.5 MB across 3,745 "
        "files, past the 128.0 MB / 10,000-file limit. 665 files were not saved."
    ),
    "tool_call_limit": (
        "The agent reached the tool-call limit for this turn. Send a message "
        "to continue where it left off."
    ),
}


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class FakeEl:
    def __init__(self, text="", tag="DIV"):
        self._text = text
        self._tag = tag
        self.clicks = 0

    async def evaluate(self, script):
        return self._tag

    async def click(self, timeout=None):
        self.clicks += 1

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def get_attribute(self, name):
        return ""

    async def inner_text(self):
        return self._text

    async def input_value(self):
        return self._text


class FakeLocator:
    def __init__(self, els):
        self._els = els

    async def count(self):
        return len(self._els)

    def nth(self, i):
        return self._els[i]

    @property
    def first(self):
        return self._els[0]


class FakePage:
    def __init__(self, body="", url="https://arena.ai/agent/01a1f791",
                 composer_selectors=("textarea",)):
        self._body = body
        self.url = url
        self.keyboard = FakeKeyboard()
        self.reloads = 0
        self.gotos = []
        self.composer = ""
        self._composer_selectors = tuple(composer_selectors)

    async def evaluate(self, script):
        return self._body

    def locator(self, sel):
        # Only the real composer matches; every other selector (consent
        # buttons, rate-limit text, stop controls) is absent.
        if sel in self._composer_selectors:
            return FakeLocator([FakeEl(self.composer)])
        return FakeLocator([])

    def get_by_text(self, label, exact=False):
        return FakeLocator([FakeEl(label)])

    async def reload(self, wait_until=None):
        self.reloads += 1

    async def goto(self, url, wait_until=None):
        self.gotos.append(url)
        self.url = url


@pytest.fixture()
def brain():
    b = BrainBrowser("arena-agent", CFG, BROWSER_CFG)
    b._page = FakePage()
    b._save_learned = lambda: None
    return b


def _run(coro):
    return asyncio.run(coro)


class TestRealMessages:
    """Every transcribed site message must map to the right remedy."""

    @pytest.mark.parametrize("cid,message", list(REAL_MESSAGES.items()))
    def test_real_message_is_recognised(self, brain, cid, message):
        brain._page = FakePage(body=message)
        cond = _run(brain._detect_condition(""))
        assert cond is not None, f"not recognised: {message!r}"
        assert cond["id"] == cid

    def test_out_of_memory_reprompts(self, brain):
        brain._page = FakePage(body=REAL_MESSAGES["out_of_memory"])
        cond = _run(brain._detect_condition(""))
        assert cond["action"] == "reprompt"

    def test_tool_call_limit_sends_continue(self, brain):
        brain._page = FakePage(body=REAL_MESSAGES["tool_call_limit"])
        cond = _run(brain._detect_condition(""))
        assert cond["action"] == "continue"
        assert cond["text"].lower() == "continue"

    def test_full_workspace_starts_a_new_session(self, brain):
        brain._page = FakePage(body=REAL_MESSAGES["workspace_full"])
        cond = _run(brain._detect_condition(""))
        assert cond["action"] == "new_session"


class TestNoFalsePositives:
    def test_phrase_already_on_the_page_is_ignored(self, brain):
        """The brain explaining a fault is not the site reporting one."""
        answer = ("I checked the workspace and it is over budget, so some "
                  "files were not saved.")
        brain._page = FakePage(body=answer)
        assert _run(brain._detect_condition(answer)) is None

    def test_a_brain_answer_about_memory_is_ignored(self, brain):
        answer = "The container ran out of memory, so I retried the build."
        brain._page = FakePage(body=answer)
        assert _run(brain._detect_condition(answer)) is None

    def test_clean_page_has_no_condition(self, brain):
        brain._page = FakePage(body="Here is the file you asked for.")
        assert _run(brain._detect_condition("")) is None

    def test_specific_condition_beats_generic(self, brain):
        body = "Something went wrong: the assistant ran out of memory."
        brain._page = FakePage(body=body)
        cond = _run(brain._detect_condition(""))
        assert cond["id"] == "out_of_memory"


class TestActions:
    def test_continue_types_the_nudge(self, brain):
        sent = []
        brain._page = FakePage()

        async def fake_send(el, text):
            sent.append(text)

        brain._type_and_send = fake_send
        assert _run(brain._act_continue("Continue")) is True
        assert sent == ["Continue"]

    def test_reprompt_resends_exactly_what_we_sent(self, brain):
        sent = []
        brain._page = FakePage()
        brain._last_typed = "[BRAIN PROXY]\ndo the thing\n\nBRDEADBEEF"

        async def fake_send(el, text):
            sent.append(text)

        brain._type_and_send = fake_send
        assert _run(brain._act_reprompt()) is True
        assert sent == ["[BRAIN PROXY]\ndo the thing\n\nBRDEADBEEF"]

    def test_reprompt_refuses_to_overwrite_the_human(self, brain):
        from errors import BrainError

        brain._page = FakePage()
        brain._page.composer = "wait, don't send that yet"
        brain._last_typed = "[BRAIN PROXY]\nold prompt"
        with pytest.raises(BrainError) as e:
            _run(brain._act_reprompt())
        assert e.value.code == "user_tab_busy"

    def test_refresh_reloads_the_tab(self, brain):
        brain._page = FakePage()
        assert _run(brain._act_refresh()) is True
        assert brain._page.reloads == 1

    def test_new_session_navigates_home_and_forgets_the_thread(self, brain):
        brain._page = FakePage(url="https://arena.ai/agent/01a1f791")
        brain._run_key = "abc123"
        brain.sessions["abc123"] = "https://arena.ai/agent/01a1f791"
        assert _run(brain._act_new_session()) is True
        assert brain._page.gotos == ["https://arena.ai/agent"]
        assert "abc123" not in brain.sessions

    def test_click_presses_the_first_matching_label(self, brain):
        brain._page = FakePage()
        assert _run(brain._act_click(["Keep working", "Continue"])) is True


class TestAttemptBudget:
    def test_exhausting_the_budget_raises(self, brain):
        from errors import BrainError

        brain._page = FakePage(body=REAL_MESSAGES["out_of_memory"])
        brain._type_and_send = lambda el, text: _async(True)
        brain._cond_counts["out_of_memory"] = 2
        with pytest.raises(BrainError) as e:
            _run(brain._recover_if_needed(""))
        assert e.value.code == "site_error"
        assert "out_of_memory" in str(e.value)

    def test_recovery_counts_up_to_the_cap(self, brain):
        brain._page = FakePage(body=REAL_MESSAGES["out_of_memory"])
        brain._type_and_send = lambda el, text: _async(True)
        assert _run(brain._recover_if_needed("")) is True
        assert brain._cond_counts["out_of_memory"] == 1
        assert _run(brain._recover_if_needed("")) is True
        assert brain._cond_counts["out_of_memory"] == 2


class TestSameSite:
    def test_same_thread_is_the_same_site(self, brain):
        assert brain._same_site("https://arena.ai/agent/01a1f791")

    def test_another_thread_on_the_same_site_is_still_the_same_site(self, brain):
        assert brain._same_site("https://arena.ai/agent/ffffffff")

    def test_a_different_site_is_not(self, brain):
        assert not brain._same_site("https://chatgpt.com/c/1")

    def test_empty_url_is_not(self, brain):
        assert not brain._same_site("")


class TestConfigExtension:
    def test_config_entries_are_appended_to_the_defaults(self):
        extra = [{"id": "custom", "phrases": ["out of credits"],
                  "action": "reprompt", "max": 1}]
        b = BrainBrowser("arena-agent", CFG, BROWSER_CFG, recovery=extra)
        ids = [c["id"] for c in b.conditions]
        assert "out_of_memory" in ids
        assert "custom" in ids
        assert ids.index("custom") > ids.index("out_of_memory")

    def test_every_default_has_a_known_action(self):
        known = {"reprompt", "continue", "refresh", "new_session", "click"}
        for cond in DEFAULT_RECOVERY:
            assert cond["action"] in known, cond
            assert cond["phrases"], cond
            assert cond["max"] >= 1, cond


class TestUrlChange:
    def test_same_site_thread_change_re_anchors(self, brain):
        brain._run_url = "https://arena.ai/agent/aaa"
        brain._run_key = "k"
        brain.sessions["k"] = "https://arena.ai/agent/aaa"
        brain._page = FakePage(url="https://arena.ai/agent/bbb")
        assert _run(brain._note_url_change()) is True
        assert brain.session_url == "https://arena.ai/agent/bbb"
        assert brain.sessions["k"] == "https://arena.ai/agent/bbb"

    def test_leaving_the_site_raises(self, brain):
        from errors import BrainError

        brain._run_url = "https://arena.ai/agent/aaa"
        brain._page = FakePage(url="https://chatgpt.com/")
        with pytest.raises(BrainError) as e:
            _run(brain._note_url_change())
        assert e.value.code == "site_changed"

    def test_no_change_is_a_no_op(self, brain):
        brain._run_url = "https://arena.ai/agent/aaa"
        brain._page = FakePage(url="https://arena.ai/agent/aaa")
        assert _run(brain._note_url_change()) is False

    def test_unknown_url_is_a_no_op(self, brain):
        brain._run_url = ""
        brain._page = FakePage(url="https://arena.ai/agent/bbb")
        assert _run(brain._note_url_change()) is False


class TestHumanIntervention:
    """Typing in the composer must not kill the turn."""

    def _counting(self, brain, failures):
        from errors import BrainError

        calls = []

        async def fake(prompt, files=None, task_id=None, client_key=None):
            calls.append(1)
            if len(calls) <= failures:
                raise BrainError("user_tab_busy", "composer in use", "arena-agent")
            return "ok"

        brain._ask_once = fake
        return calls

    def test_busy_composer_is_waited_for(self, brain):
        brain.busy_wait_base = 0
        calls = self._counting(brain, failures=2)
        assert _run(brain.ask("hi")) == "ok"
        assert len(calls) == 3

    def test_busy_composer_gives_up_after_three_waits(self, brain):
        from errors import BrainError

        brain.busy_wait_base = 0
        self._counting(brain, failures=99)
        with pytest.raises(BrainError) as e:
            _run(brain.ask("hi"))
        assert e.value.code == "user_tab_busy"

    def test_busy_composer_does_not_trip_the_circuit_breaker(self, brain):
        brain.busy_wait_base = 0
        failed = []
        brain.breaker = type("B", (), {
            "fail": staticmethod(lambda *a: failed.append(a)),
            "ok": staticmethod(lambda: None),
        })()
        self._counting(brain, failures=1)
        assert _run(brain.ask("hi")) == "ok"
        assert failed == []


class TestDeadBrowserRecovery:
    """A killed browser must not wedge the proxy forever."""

    def test_a_live_context_reads_as_alive(self, brain):
        class Browser:
            def is_connected(self):
                return True

        brain._context = type("C", (), {"browser": Browser()})()
        assert _run(brain._context_alive()) is True

    def test_a_closed_browser_reads_as_dead(self, brain):
        class Browser:
            def is_connected(self):
                return False

        brain._context = type("C", (), {"browser": Browser()})()
        assert _run(brain._context_alive()) is False

    def test_a_context_with_no_browser_reads_as_dead(self, brain):
        brain._context = type("C", (), {"browser": None})()
        assert _run(brain._context_alive()) is False

    def test_an_exploding_property_reads_as_dead(self, brain):
        class Ctx:
            @property
            def browser(self):
                raise RuntimeError("TargetClosedError")

        brain._context = Ctx()
        assert _run(brain._context_alive()) is False

    def test_dropping_a_dead_context_clears_every_handle(self, brain,
                                                         monkeypatch):
        import browser as browser_mod

        closed = []

        class Ctx:
            browser = None

            async def close(self):
                closed.append(True)

        stale = Ctx()
        brain._context = stale
        brain._pw = object()
        brain.ready = True
        brain._page = object()
        monkeypatch.setitem(browser_mod._SHARED, "ctx", stale)
        monkeypatch.setitem(browser_mod._SHARED, "pw", object())

        _run(brain._drop_dead_context())

        assert brain._context is None
        assert brain._pw is None
        assert brain.ready is False
        assert brain._page is None
        assert browser_mod._SHARED["ctx"] is None
        assert browser_mod._SHARED["pw"] is None
        assert closed == [True]


    def test_no_context_means_no_relaunch(self, brain):
        brain._context = None
        assert _run(brain.browser_needs_relaunch()) is False

    def test_a_dead_browser_means_relaunch(self, brain):
        class Browser:
            def is_connected(self):
                return False

        brain._context = type("C", (), {"browser": Browser()})()
        assert _run(brain.browser_needs_relaunch()) is True


class TestLockedProfileRecovery:
    """A profile left locked by a killed browser must not block the relaunch."""

    def test_a_locked_profile_is_cleared_and_retried(self, brain, monkeypatch):
        calls = []
        cleared = []

        class PW:
            class chromium:
                @staticmethod
                async def launch_persistent_context(**kw):
                    calls.append(kw)
                    if len(calls) == 1:
                        raise RuntimeError(
                            "BrowserType.launch_persistent_context: Opening "
                            "in existing browser session.")
                    return "CTX"

        brain._pw = PW()
        monkeypatch.setattr(brain, "_kill_profile_holders",
                            lambda: cleared.append(True))

        assert _run(brain._launch_browser()) == "CTX"
        assert len(calls) == 2
        assert cleared == [True]

    def test_a_sandbox_failure_falls_back_to_no_sandbox(self, brain):
        calls = []

        class PW:
            class chromium:
                @staticmethod
                async def launch_persistent_context(**kw):
                    calls.append(kw["chromium_sandbox"])
                    if kw["chromium_sandbox"]:
                        raise RuntimeError("sandbox unavailable")
                    return "CTX"

        brain._pw = PW()
        assert _run(brain._launch_browser()) == "CTX"
        assert calls == [True, False]

    def test_the_launch_passes_no_automation_arguments(self, brain):
        seen = []

        class PW:
            class chromium:
                @staticmethod
                async def launch_persistent_context(**kw):
                    seen.append(kw)
                    return "CTX"

        brain._pw = PW()
        _run(brain._launch_browser())
        assert seen[0]["args"] == []
        assert "--disable-blink-features=AutomationControlled" in \
            seen[0]["ignore_default_args"]


class TestClassification:
    def test_a_bare_timeout_is_not_reported_as_a_site_change(self):
        import asyncio

        from errors import classify

        # asyncio.TimeoutError() has an empty str(), so it can only be
        # recognised by type. Reporting it as site_changed would bench the
        # brain for 15 minutes over a merely slow reply.
        assert classify(asyncio.TimeoutError()) == "hard_timeout"

    def test_hard_timeout_is_transient_with_a_short_cooldown(self):
        from errors import COOLDOWN, TRANSIENT

        assert "hard_timeout" in TRANSIENT
        assert COOLDOWN["hard_timeout"] <= 120


async def _async(value):
    return value
