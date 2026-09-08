"""In-process sliding-window rate limiter.

Single-instance only: the counters below live in this process's memory, so they reset on
deploy/restart and don't share state across multiple Cloud Run instances. That's an accepted
gap for launch (see crewlee-be/CLAUDE.md) -- there's no Redis/shared cache in this stack yet.
Good enough to blunt casual abuse of the public Guest AI endpoint; not a substitute for a
real distributed limiter if this app ever scales to multiple concurrent instances.
"""
import time
from collections import defaultdict

_hits: dict[str, list[float]] = defaultdict(list)


def check(key: str, max_requests: int, window_seconds: int) -> bool:
    """Records one hit for `key` and returns whether it's still within the limit."""
    now = time.monotonic()
    cutoff = now - window_seconds
    bucket = _hits[key]
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_requests:
        return False
    bucket.append(now)
    return True
