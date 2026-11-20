"""Tests for the two interactions where the proxy must not hurt the human.

1. The composer guard: text the user typed must never be overwritten.
2. Arena's feedback dialog: it must be answered with "Keep working".
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser import BrainBrowser  # noqa: E402

CFG = {
    "url": "https://chatgpt.com/",
    "input_selectors": ["textarea"],
    "response_selectors": ["[data-msg]"],
}
BROWSER_CFG = {"user_data_dir": "/tmp/brain-proxy-test-profile2"}


@pytest.fixture()
def brain():
    return BrainBrowser("chatgpt", CFG, BROWSER_CFG)


class TestComposerGuard:
    def test_empty_composer_is_fine(self, brain):
        assert brain._composer_text_is_ours("")

    def test_short_user_text_is_not_ours(self, brain):
        """Regression: short user messages used to be silently overwritten."""
        for text in ["hi", "stop", "wait", "no", "ok", "x"]:
            assert not brain._composer_text_is_ours(text), text

    def test_long_user_text_is_not_ours(self, brain):
        assert not brain._composer_text_is_ours(
            "please do not send this yet, I am still typing")

    def test_our_own_leftover_is_recognised(self, brain):
        brain._last_typed = "[BRAIN PROXY]\nsome prompt\n\nBRDEADBEEF"
        assert brain._composer_text_is_ours("[BRAIN PROXY]\nsome prompt")

    def test_our_marker_is_recognised(self, brain):
        """A later-turn prompt carries the marker but no preamble."""
        brain._prev_marker = "BRDEADBEEF"
        assert brain._composer_text_is_ours("some text BRDEADBEEF")

    def test_an_unrelated_marker_is_not_ours(self, brain):
        """We only trust the marker we actually used for the last send."""
        brain._prev_marker = "BRDEADBEEF"
        assert not brain._composer_text_is_ours("some text BRAADF00D")

    def test_the_current_marker_is_trusted(self, brain):
        """A later-turn prompt has the marker and nothing else to identify it.

        This is the one accepted trade-off: text containing the exact marker
        we last used is treated as ours. The marker is 8 random hex chars,
        so the human typing it by hand is not a realistic scenario.
        """
        brain._prev_marker = "BRDEADBEEF"
        assert brain._composer_text_is_ours("BRDEADBEEF is what I want")

    def test_our_preamble_is_recognised(self, brain):
        assert brain._composer_text_is_ours(
            "[BRAIN PROXY]\nYou are the reasoning engine inside an agent framework.")

    def test_a_short_word_inside_our_prompt_is_not_ours(self, brain):
        """Regression: 'ok' typed by the human must not be deleted just
        because the word happens to appear in our previous prompt."""
        brain._last_typed = "[BRAIN PROXY]\nlook at the ok button\n\nBRDEADBEEF"
        brain._prev_marker = "BRDEADBEEF"
        assert not brain._composer_text_is_ours("ok")

    def test_a_leftover_prefix_from_an_interrupted_send_is_ours(self, brain):
        """Regression: a killed send leaves a short prefix in the composer.

        It is ours by construction and must be cleared - otherwise the brain
        wedges permanently on user_tab_busy.
        """
        brain._last_typed = "[BRAIN PROXY]\n" + ("word " * 200) + "\n\nBRDEADBEEF"
        brain._prev_marker = "BRDEADBEEF"
        assert brain._composer_text_is_ours("[BRAIN PROXY]\nword word word")
        assert brain._composer_text_is_ours("[BRAIN PROXY]\n")
        assert brain._composer_text_is_ours("[BRAIN PROXY]")

    def test_a_human_message_that_is_not_a_prefix_is_still_refused(self, brain):
        brain._last_typed = "[BRAIN PROXY]\nplease analyse the logs\n\nBRDEADBEEF"
        brain._prev_marker = "BRDEADBEEF"
        assert not brain._composer_text_is_ours("please analyse the servers")


class FakeClickable:
    def __init__(self, text="", visible=True):
        self._text = text
        self._visible = visible
        self.clicked = False

    async def inner_text(self):
        return self._text

    async def is_visible(self):
        return self._visible

    async def click(self, timeout=None):
        self.clicked = True


class FakeLocator:
    def __init__(self, elements):
        self._elements = elements

    async def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i]

    @property
    def first(self):
        return self._elements[0]


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class FakePage2:
    """A page where a locator selector maps to a list of elements."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.keyboard = FakeKeyboard()
        self.url = "https://arena.ai/agent"

    def locator(self, sel):
        return FakeLocator(self.mapping.get(sel, []))


class TestArenaFeedback:
    def _brain_with(self, mapping):
        b = BrainBrowser("arena-agent", CFG, BROWSER_CFG)
        b._page = FakePage2(mapping)
        return b

    def test_keep_working_is_clicked(self):
        keep = FakeClickable()
        b = self._brain_with({
            "text=Was this task successful?": [FakeClickable("?")],
            "text=Keep working": [keep],
        })
        assert asyncio.run(b._handle_arena_feedback()) is True
        assert keep.clicked

    def test_no_button_falls_back_to_escape(self):
        b = self._brain_with({
            "text=Was this task successful?": [FakeClickable("?")],
        })
        assert asyncio.run(b._handle_arena_feedback()) is False
        assert "Escape" in b._page.keyboard.pressed

    def test_no_dialog_is_a_no_op(self):
        b = self._brain_with({})
        assert asyncio.run(b._handle_arena_feedback()) is False
        assert b._page.keyboard.pressed == []

    def test_never_clicks_no(self):
        """'No' offers an unreliable undo, so it must never be pressed."""
        no = FakeClickable()
        b = self._brain_with({
            "text=Was this task successful?": [FakeClickable("?")],
            "text=Keep working": [FakeClickable()],
            "text=No": [no],
            "button:has-text('No')": [no],
        })
        asyncio.run(b._handle_arena_feedback())
        assert not no.clicked
