"""The browser must not announce that it is automated.

Both of these flags are well-known bot fingerprints: Brave prints a warning
banner for either, and the banner is visible on every page the proxy drives.

The check walks the AST rather than grepping the source, so a comment that
merely mentions a flag does not trip it - only an actual string literal passed
to the browser does.
"""

import ast
import inspect
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser import IGNORE_DEFAULT_ARGS, LAUNCH_ARGS, BrainBrowser  # noqa: E402

FORBIDDEN = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-web-security",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
)


def _tree(func):
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


class TestNoAutomationFlags:
    def test_connect_passes_no_known_automation_flag(self):
        tree = _tree(BrainBrowser._connect)
        constants = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for flag in FORBIDDEN:
            offenders = [c for c in constants if flag in c]
            assert not offenders, (
                f"{flag} is an automation fingerprint and must not be passed "
                f"to the browser; found in {offenders!r}"
            )

    def test_the_launch_argument_list_defaults_to_empty(self):
        assert LAUNCH_ARGS == []
        tree = _tree(BrainBrowser._launch_browser)
        passed = [
            kw.value.id for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "args"
        ]
        assert passed == ["LAUNCH_ARGS"], \
            f"the launch should pass LAUNCH_ARGS, got {passed!r}"

    def test_playwrights_own_automation_flag_is_dropped(self):
        """Playwright re-adds this flag even when args is empty.

        It has to be removed with ignore_default_args, or Brave prints its
        warning banner on every page.
        """
        tree = _tree(BrainBrowser._launch_browser)
        kwargs = [
            kw.arg for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
        ]
        assert "ignore_default_args" in kwargs
        assert "--disable-blink-features=AutomationControlled" in \
            IGNORE_DEFAULT_ARGS
