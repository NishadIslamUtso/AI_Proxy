"""Failure taxonomy for brain-proxy.

Every failure gets exactly one code so the retry policy, the circuit
breaker and the telemetry can all key off the same string.
"""


class BrainError(RuntimeError):
    def __init__(self, code, msg="", brain=""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.brain = brain


# Worth retrying immediately (usually a stale tab or a dropped socket).
TRANSIENT = {"upstream_offline", "cdp_dead", "tab_dead", "send_failed",
             "no_reply", "site_error"}

# Retrying will not help; the human has to do something or the brain must be skipped.
FAIL_FAST = {"auth_wall", "captcha", "rate_limited", "site_changed", "user_tab_busy",
             "consent_wall", "paused"}

# How long a brain stays benched when this code trips the breaker.
COOLDOWN = {
    "rate_limited": 1800,
    "captcha": 600,
    "auth_wall": 600,
    "site_changed": 900,
}


def classify(exc, page_title=""):
    """Map an exception (plus the current page title) to a failure code."""
    s = str(exc).lower()
    t = (page_title or "").lower()

    if "just a moment" in t or "cloudflare" in t or "verify you are human" in t:
        return "captcha"
    if any(k in s for k in ("err_internet_disconnected", "err_name_not_resolved",
                            "err_connection", "net::err", "connection refused",
                            "getaddrinfo", "name or service not known")):
        return "upstream_offline"
    if any(k in s for k in ("target closed", "browser has been closed", "browser closed",
                            "websocket", "cdp", "connection closed")):
        return "cdp_dead"
    if "user_tab_busy" in s:
        return "user_tab_busy"
    if any(k in s for k in ("sign in", "sign_in", "log in", "login", "signin")):
        return "auth_wall"
    if any(k in s for k in ("daily usage limit", "rate limit", "too many requests", "quota")):
        return "rate_limited"
    if any(k in s for k in ("no reply", "empty reply", "no selector", "no input box",
                            "composer not found", "reply was empty")):
        return "no_reply"
    if any(k in s for k in ("timeout", "timed out", "exceeded",
                            "timeouterror")):
        return "no_reply"
    return "site_changed"


def as_openai_error(code, msg=""):
    """OpenAI-shaped error body so Hermes renders it properly."""
    return {"error": {"message": msg or code, "type": "brain_proxy_error", "code": code}}
