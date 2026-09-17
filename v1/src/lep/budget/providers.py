"""Provider limits, and reading consumption from response headers.

Section 3.3: **read consumption from the provider, not from your own counter.**
Our own count drifts and, more importantly, misses every other integration on
the account -- which is exactly the thing that matters, because the budget is
shared.

The numbers below are the documented published limits as of the design
document, and section 3.1 says to verify them against the actual org:
Setup -> Company Information -> API Requests, Last 24 Hours.  Treat
:data:`SALESFORCE_EDITIONS` as a default that a real deployment overrides from
``observe_headers``.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Mapping

# Section 3.1.  base, per-Salesforce-license, hard cap.
SALESFORCE_EDITIONS: dict[str, tuple[int, int, int | None]] = {
    "enterprise": (100_000, 1_000, None),
    "unlimited": (100_000, 5_000, None),
    "performance": (100_000, 5_000, None),
    "developer": (15_000, 0, 15_000),
}

# Section 3.1.  burst per 10s, daily.
HUBSPOT_PLANS: dict[str, tuple[int, int]] = {
    "free": (100, 250_000),
    "starter": (100, 250_000),
    "professional": (190, 625_000),
    "enterprise": (190, 1_000_000),
    "oauth_marketplace": (110, 250_000),
}

#: The CRM Search API is far tighter than the general limit, and matching is
#: exactly when you want to search (section 3.1).
HUBSPOT_SEARCH_PER_SECOND = 5

# Section 3.1, Bulk API 2.0.
BULK_MAX_BYTES_PER_JOB = 150 * 1024 * 1024
BULK_PRACTICAL_BYTES_PER_JOB = 100 * 1024 * 1024
BULK_RECORDS_PER_BATCH = 10_000
BULK_BATCHES_PER_DAY = 15_000
BULK_CONCURRENT_JOBS = 25
REST_BATCH_SIZE = 200          # WHERE Id IN (...) composite read
BULK_THRESHOLD_RECORDS = 2_000  # above this, one Bulk job beats N REST calls


def salesforce_daily_limit(edition: str, licenses: int = 0, add_ons: int = 0) -> int:
    """``base + (per-license x licenses) + add-ons``, capped where applicable."""
    base, per_license, hard_cap = SALESFORCE_EDITIONS[edition.lower()]
    total = base + per_license * licenses + add_ons
    return min(total, hard_cap) if hard_cap else total


def hubspot_daily_limit(plan: str) -> int:
    return HUBSPOT_PLANS[plan.lower()][1]


@dataclass(frozen=True)
class UsageSnapshot:
    """What the provider says about consumption right now."""

    used: int
    limit: int
    provider: str
    remaining_burst: int | None = None
    source: str = "header"

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def fraction_used(self) -> float:
        return 0.0 if self.limit <= 0 else self.used / self.limit


_SFORCE_RE = re.compile(r"api-usage\s*=\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def parse_sforce_limit_info(headers: Mapping[str, str]) -> UsageSnapshot | None:
    """Parse ``Sforce-Limit-Info: api-usage=18/5000``.

    Note that this figure is the whole org's pooled consumption across REST,
    SOAP, Bulk and Connect -- every integration on the account, not just ours.
    That is the point of reading it (section 3.2).
    """
    raw = _get(headers, "sforce-limit-info")
    if not raw:
        return None
    match = _SFORCE_RE.search(raw)
    if not match:
        return None
    return UsageSnapshot(
        used=int(match.group(1)), limit=int(match.group(2)), provider="salesforce"
    )


def parse_hubspot_headers(headers: Mapping[str, str]) -> UsageSnapshot | None:
    """Parse ``X-HubSpot-RateLimit-*``.

    The daily figure is shared across every app installed on the account.
    """
    daily = _get(headers, "x-hubspot-ratelimit-daily")
    daily_remaining = _get(headers, "x-hubspot-ratelimit-daily-remaining")
    burst_remaining = _get(headers, "x-hubspot-ratelimit-remaining")
    if daily is None or daily_remaining is None:
        return None
    limit = int(daily)
    remaining = int(daily_remaining)
    return UsageSnapshot(
        used=max(0, limit - remaining),
        limit=limit,
        provider="hubspot",
        remaining_burst=int(burst_remaining) if burst_remaining is not None else None,
    )


def parse_headers(provider: str, headers: Mapping[str, str]) -> UsageSnapshot | None:
    if provider == "salesforce":
        return parse_sforce_limit_info(headers)
    if provider == "hubspot":
        return parse_hubspot_headers(headers)
    return None


def _get(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class BurstLimiter:
    """Token bucket for short-window limits.

    HubSpot's general limit is per 10 seconds; its CRM Search API is 5 per
    *second*, which is the one that bites during matching.
    """

    def __init__(self, capacity: int, per_seconds: float, clock=time.monotonic) -> None:
        self.capacity = capacity
        self.per_seconds = per_seconds
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()
        self._lock = threading.Lock()

    @classmethod
    def hubspot_search(cls, clock=time.monotonic) -> "BurstLimiter":
        return cls(HUBSPOT_SEARCH_PER_SECOND, 1.0, clock=clock)

    @classmethod
    def hubspot_general(cls, plan: str = "professional", clock=time.monotonic) -> "BurstLimiter":
        return cls(HUBSPOT_PLANS[plan.lower()][0], 10.0, clock=clock)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(
            self.capacity, self._tokens + elapsed * (self.capacity / self.per_seconds)
        )
        self._updated = now

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def delay_for(self, n: int = 1) -> float:
        """Seconds to wait before ``n`` tokens are available."""
        with self._lock:
            self._refill()
            if self._tokens >= n:
                return 0.0
            deficit = n - self._tokens
            return deficit / (self.capacity / self.per_seconds)

    def acquire(self, n: int = 1, sleep=time.sleep) -> float:
        waited = 0.0
        while True:
            delay = self.delay_for(n)
            if delay <= 0 and self.try_acquire(n):
                return waited
            sleep(delay)
            waited += delay
