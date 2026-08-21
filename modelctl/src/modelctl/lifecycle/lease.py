from __future__ import annotations

import time


def parse_ttl(ttl: str) -> int:
    # e.g. "2h", "30m", "1d", "3600"
    ttl = ttl.strip().lower()
    if ttl.isdigit():
        return int(ttl)
    unit = ttl[-1]
    num = ttl[:-1]
    if not num.isdigit():
        raise ValueError(f"invalid ttl: {ttl}")
    n = int(num)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    raise ValueError(f"invalid ttl: {ttl}")


def lease_expired(started_at_ts: float, ttl_s: int | None) -> bool:
    if ttl_s is None:
        return False
    return (time.time() - started_at_ts) > ttl_s
