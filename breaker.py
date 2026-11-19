"""Per-brain circuit breaker.

closed   -> normal
open     -> brain is benched; skip it and escalate
half_open-> one probe attempt allowed after the cooldown
"""

import time


class Breaker:
    def __init__(self, threshold=3, default_cooldown=300):
        self.n = 0
        self.threshold = threshold
        self.default_cooldown = default_cooldown
        self.until = 0.0
        self.state = "closed"
        self.last_code = ""

    def allow(self):
        if self.state == "open" and time.time() > self.until:
            self.state = "half_open"
            return True
        return self.state != "open"

    def ok(self):
        self.n = 0
        self.state = "closed"
        self.last_code = ""

    def fail(self, code, cooldown=None):
        self.n += 1
        if self.n >= self.threshold or cooldown is not None:
            cd = cooldown if cooldown is not None else self.default_cooldown
            self.state = "open"
            self.until = time.time() + cd
            self.last_code = code
        return self.state

    def as_dict(self):
        return {
            "state": self.state,
            "strikes": self.n,
            "retry_after": max(0, int(self.until - time.time())) if self.state == "open" else 0,
            "last_code": self.last_code,
        }


class BreakerSet:
    def __init__(self, **kw):
        self.kw = kw
        self._items = {}

    def get(self, name):
        if name not in self._items:
            self._items[name] = Breaker(**self.kw)
        return self._items[name]

    def allow(self, name):
        return self.get(name).allow()

    def as_dict(self):
        return {k: v.as_dict() for k, v in self._items.items()}
