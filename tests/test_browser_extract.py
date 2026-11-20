"""Tests for the browser adapter's text handling.

The reply cleanup and extraction are the two places where a correct brain
answer most easily gets mangled, so they get characterisation tests of
their own - no browser required.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser import BrainBrowser  # noqa: E402


BRAINS = {
    "chatgpt": {
        "url": "https://chatgpt.com/",
        "input_selectors": ["textarea"],
        "response_selectors": ["div[data-message-author-role='assistant']"],
        "submit_key": "Enter",
    },
    "arena-chat": {
        "url": "https://arena.ai/",
        "input_selectors": ["textarea"],
        "response_selectors": ["[class*=markdown]"],
    },
}

BROWSER_CFG = {
    "user_data_dir": "/tmp/brain-proxy-test-profile",
    "executable_path": "",
}


@pytest.fixture()
def brain():
    return BrainBrowser("chatgpt", BRAINS["chatgpt"], BROWSER_CFG)


class TestCleanKeepsRealAnswers:
    """Regression: the old cleanup ate the first paragraph of good answers."""

    @pytest.mark.parametrize("opener", [
        "We need to fix the parser.",
        "The user wants a summary of the logs.",
        "I need to check the config file first.",
        "Let me walk through the options.",
        "Okay, here is the plan.",
        "The task is to deploy the service.",
        "Same task, different input.",
        "I should start by reading the manifest.",
    ])
    def test_opening_paragraph_survives(self, brain, opener):
        answer = f"{opener}\n\nHere is the detail you asked for."
        out = brain._clean(answer)
        assert opener in out, f"the opening paragraph was eaten: {out!r}"
        assert "Here is the detail you asked for." in out

    def test_single_paragraph_answer_is_untouched(self, brain):
        answer = "We need to fix the parser."
        assert brain._clean(answer) == answer

    def test_whole_answer_kept_when_it_starts_with_we_need(self, brain):
        answer = ("We need to fix the parser.\n\n"
                  "The bug is in _clean, which strips any paragraph that starts "
                  "with a list of magic words.\n\n"
                  "Here is the patch.")
        out = brain._clean(answer)
        assert out.count("\n\n") == 2, f"a paragraph was removed: {out!r}"


class TestCleanRemovesEchoes:
    """The cleanup should still remove a genuine echo of our prompt."""

    def test_prompt_echo_is_removed(self, brain):
        brain._last_typed = (
            "[BRAIN PROXY]\nYou are the reasoning engine inside an agent "
            "framework. Reply with exactly one JSON object and nothing else: "
            "{\"tool\": \"<name>\", \"args\": {...}}.\n\nBRDEADBEEF"
        )
        echo = ("You are the reasoning engine inside an agent framework. Reply "
                "with exactly one JSON object and nothing else.")
        answer = f"{echo}\n\nHere is the answer."
        out = brain._clean(answer)
        assert out == "Here is the answer."

    def test_acknowledgement_is_removed(self, brain):
        answer = "Understood.\n\nHere is the answer."
        assert brain._clean(answer) == "Here is the answer."

    def test_format_instruction_is_removed(self, brain):
        answer = ("Reply with exactly one JSON object and nothing else.\n\n"
                  '{"tool": "run_cmd", "args": {"command": "ls"}}')
        out = brain._clean(answer)
        assert out.startswith("{")

    def test_real_answer_with_reply_word_is_kept(self, brain):
        """'I need to reply to the customer' is an answer, not an echo."""
        answer = ("I need to reply to the customer with the tracking number.\n\n"
                  "The number is 4471.")
        out = brain._clean(answer)
        assert "tracking number" in out

    def test_footer_is_stripped(self, brain):
        answer = "The answer.\n\nChatGPT can make mistakes. Check important info."
        assert brain._clean(answer) == "The answer."


class FakeElement:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text


class FakeLocator:
    def __init__(self, texts):
        self._texts = texts

    async def count(self):
        return len(self._texts)

    def nth(self, i):
        # Playwright's Locator.nth() is synchronous and returns a Locator.
        return FakeElement(self._texts[i])


class FakePage:
    def __init__(self, bubbles, body=""):
        self.bubbles = bubbles
        self.body = body

    def locator(self, sel):
        return FakeLocator(self.bubbles)

    async def evaluate(self, _script):
        return self.body


class TestExtractRejectsStaleReplies:
    """The reply we extract must be new, not the previous turn's answer."""

    def test_previous_answer_is_not_returned(self, brain):
        brain.response_selectors = ["[data-msg]"]
        brain._page = FakePage(["the answer from last turn"], body="page text")
        brain._pre_bubbles = ["the answer from last turn"]
        brain._marker = "BRDEADBEEF"
        out = asyncio.run(brain._extract("page text"))
        assert "the answer from last turn" not in out

    def test_new_bubble_is_returned(self, brain):
        brain.response_selectors = ["[data-msg]"]
        brain._page = FakePage(["old answer", "new answer"], body="page text")
        brain._pre_bubbles = ["old answer"]
        brain._marker = "BRDEADBEEF"
        out = asyncio.run(brain._extract("page text"))
        assert "new answer" in out
        assert "old answer" not in out

    def test_changed_bubble_is_returned(self, brain):
        """Streaming / agent mode grows the same bubble in place."""
        brain.response_selectors = ["[data-msg]"]
        brain._page = FakePage(["partial answer"], body="page text")
        brain._pre_bubbles = ["partial"]
        brain._marker = "BRDEADBEEF"
        out = asyncio.run(brain._extract("page text"))
        assert "partial answer" in out

    def test_marker_fallback_is_used_when_nothing_is_new(self, brain):
        brain.response_selectors = ["[data-msg]"]
        brain._page = FakePage(["old answer"], body="prompt BRDEADBEEF fresh reply")
        brain._pre_bubbles = ["old answer"]
        brain._marker = "BRDEADBEEF"
        out = asyncio.run(brain._extract("prompt BRDEADBEEF fresh reply"))
        assert "fresh reply" in out


class TestSnapshot:
    def test_snapshot_returns_bubble_texts(self, brain):
        brain.response_selectors = ["[data-msg]"]
        brain._page = FakePage(["a", "b"])
        assert asyncio.run(brain._response_snapshot()) == ["a", "b"]

    def test_snapshot_of_missing_selector_is_empty(self, brain):
        brain.response_selectors = []
        brain._page = FakePage(["a"])
        assert asyncio.run(brain._response_snapshot()) == []


class TestTaskKey:
    def test_task_key_is_stable_for_the_same_prompt(self, brain):
        assert brain._task_key("abc") == brain._task_key("abc")

    def test_task_key_differs_for_different_prompts(self, brain):
        assert brain._task_key("abc") != brain._task_key("abd")


class TestSameUrl:
    def test_trailing_slash_is_ignored(self, brain):
        assert brain._same_url("https://chatgpt.com/c/1", "https://chatgpt.com/c/1/")

    def test_query_is_ignored(self, brain):
        assert brain._same_url("https://chatgpt.com/c/1?x=1",
                               "https://chatgpt.com/c/1")

    def test_different_threads_differ(self, brain):
        assert not brain._same_url("https://chatgpt.com/c/1",
                                   "https://chatgpt.com/c/2")
