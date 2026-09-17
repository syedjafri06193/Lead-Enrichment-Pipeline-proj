"""HubSpot adapter (design.md sections 3.1, 7.3).

Two things shape the design:

* the daily limit is **shared across every app installed on the account**, so
  our consumption is taken from the customer's other integrations;
* the CRM Search API is 5 requests per *second* -- far tighter than the general
  limit, and matching is exactly when you want to search.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lep.budget.providers import HUBSPOT_PLANS, HUBSPOT_SEARCH_PER_SECOND, BurstLimiter
from lep.sync.crm import FakeCrm


class FakeHubSpot(FakeCrm):
    def __init__(self, *, plan: str = "professional", **kwargs: Any) -> None:
        burst, daily = HUBSPOT_PLANS[plan.lower()]
        kwargs.setdefault("integration_user_id", "hubspot-private-app-1")
        super().__init__("hubspot", daily_limit=daily, **kwargs)
        self.plan = plan
        self.burst = burst
        self.search_limiter = BurstLimiter.hubspot_search()
        self.search_calls = 0
        self.search_throttled = 0

    def headers(self) -> dict[str, str]:
        return {
            "X-HubSpot-RateLimit-Daily": str(self.daily_limit),
            "X-HubSpot-RateLimit-Daily-Remaining": str(
                max(0, self.daily_limit - self.calls)
            ),
            "X-HubSpot-RateLimit-Max": str(self.burst),
            "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
        }

    def search(self, predicate, limit: int = 100) -> list[tuple[str, dict[str, Any]]]:
        """CRM Search: 5 per second, and it shares the daily cap.

        Returns an empty list when throttled rather than raising: the caller
        should back off and retry, not treat it as "no results".
        """
        if not self.search_limiter.try_acquire():
            self.search_throttled += 1
            return []
        self.calls += 1
        self.search_calls += 1
        return [
            (r.record_id, dict(r.fields))
            for r in self.records.values()
            if not r.deleted and predicate(r.fields)
        ][:limit]

    def modified_since(self, since: datetime) -> list[tuple[str, dict[str, Any]]]:
        """``hs_lastmodifieddate`` search -- the pull path (section 7.3)."""
        return self.updated_since(since)


SEARCH_PER_SECOND = HUBSPOT_SEARCH_PER_SECOND
