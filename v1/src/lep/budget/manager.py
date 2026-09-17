"""The shared-resource governor (design.md section 3.3).

You are spending an API budget you do not own, pooled with every other
integration the customer has.  Exhausting it returns HTTP 403 on Salesforce and
stops *everything* on the account -- their marketing automation, their billing
sync, their warehouse ETL -- not just this feature.

So the budget is modelled as a resource with a reserve, priority classes, and
consumption read from the provider rather than counted locally.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Callable, Iterable, Literal, Mapping

from lep.budget.providers import (
    BULK_RECORDS_PER_BATCH,
    BULK_THRESHOLD_RECORDS,
    REST_BATCH_SIZE,
    BurstLimiter,
    UsageSnapshot,
    parse_headers,
)
from lep.core.types import utcnow

Provider = Literal["salesforce", "hubspot"]


class BudgetExhausted(Exception):
    """Raised instead of making a call that would eat into the reserve."""

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Priority(IntEnum):
    """Not FIFO.  When budget is tight, real-time work runs and backfill waits.

    Section 3.3: "Prioritize.  When budget is tight, real-time user-triggered
    operations run and background backfill pauses."
    """

    REALTIME = 30      # a user is waiting
    INCREMENTAL = 20   # webhook-driven sync
    ENRICHMENT = 10    # provider calls, deferrable
    BACKFILL = 0       # lowest; pauses first


#: Fraction of the *usable* budget each class may consume.  A backfill that can
#: take the whole 70% is a backfill that will take down the customer during
#: quarter end (section 12.2).
DEFAULT_CLASS_SHARE: dict[Priority, float] = {
    Priority.REALTIME: 1.00,
    Priority.INCREMENTAL: 0.90,
    Priority.ENRICHMENT: 0.60,
    Priority.BACKFILL: 0.35,
}


@dataclass
class ApiBudget:
    """One provider's rolling 24-hour allocation for one org."""

    org_id: str
    provider: Provider
    daily_limit: int
    #: Never consume the customer's last 30%.  It belongs to their other
    #: integrations and to unexpected load.
    reserve_fraction: float = 0.30
    consumed: int = 0
    window_start: datetime = field(default_factory=utcnow)
    #: Consumption as last reported by the provider, which includes every other
    #: app on the account.
    external_consumed: int = 0
    last_snapshot: UsageSnapshot | None = None
    #: Per-priority-class consumption, so backfill cannot eat the whole budget.
    consumed_by: dict["Priority", int] = field(default_factory=dict)

    @property
    def usable(self) -> int:
        return int(self.daily_limit * (1 - self.reserve_fraction))

    @property
    def total_consumed(self) -> int:
        """Provider-reported consumption wins when we have it."""
        if self.last_snapshot is not None:
            return max(self.last_snapshot.used, self.consumed)
        return self.consumed + self.external_consumed

    @property
    def available(self) -> int:
        return max(0, self.usable - self.total_consumed)

    @property
    def fraction_used(self) -> float:
        return 0.0 if self.daily_limit <= 0 else self.total_consumed / self.daily_limit

    def roll_window(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if now - self.window_start >= timedelta(hours=24):
            self.window_start = now
            self.consumed = 0
            self.external_consumed = 0
            self.last_snapshot = None
            return True
        return False

    def available_for(self, priority: "Priority", shares: Mapping["Priority", float]) -> int:
        cap = int(self.usable * shares.get(priority, 1.0))
        return max(0, min(self.available, cap - self.consumed_by.get(priority, 0)))


@dataclass(frozen=True)
class ReadPlan:
    """How to fetch ``n`` records without being the reason support gets called."""

    records: int
    strategy: Literal["rest_single", "rest_batched", "bulk"]
    calls: int
    jobs: int = 0
    note: str = ""


def plan_reads(records: int, *, batch_size: int = REST_BATCH_SIZE) -> ReadPlan:
    """Section 3.4 arithmetic, as code.

    500k records: 500,000 individual REST calls (impossible), 2,500 batched, or
    about 50 Bulk jobs.
    """
    if records <= 0:
        return ReadPlan(0, "rest_batched", 0, note="nothing to do")
    if records > BULK_THRESHOLD_RECORDS:
        # ~10k records per job keeps each upload well under the 150 MB limit
        # and matches the section 3.4 arithmetic (500k records -> ~50 jobs).
        jobs = math.ceil(records / BULK_RECORDS_PER_BATCH)
        return ReadPlan(
            records,
            "bulk",
            calls=jobs * 3,  # create + upload + close, plus polling in practice
            jobs=jobs,
            note=f"Bulk API 2.0: {jobs} job(s) instead of "
            f"{math.ceil(records / batch_size)} REST calls",
        )
    calls = math.ceil(records / batch_size)
    return ReadPlan(
        records,
        "rest_batched",
        calls=calls,
        note=f"batched at {batch_size}: {calls} calls instead of {records}",
    )


class BudgetManager:
    """Gatekeeper for every outbound CRM call.

    Usage::

        if not budget.reserve(provider="salesforce", calls=1, priority=Priority.REALTIME):
            ...                      # defer, do not call
        response = client.get(...)
        budget.observe_headers("salesforce", response.headers)
    """

    def __init__(
        self,
        org_id: str,
        budgets: Iterable[ApiBudget] | None = None,
        *,
        class_share: Mapping[Priority, float] | None = None,
        clock: Callable[[], datetime] = utcnow,
        limiters: Mapping[str, BurstLimiter] | None = None,
        on_report: Callable[[str], None] | None = None,
    ) -> None:
        self.org_id = org_id
        self.budgets: dict[str, ApiBudget] = {b.provider: b for b in (budgets or [])}
        self.class_share = dict(class_share or DEFAULT_CLASS_SHARE)
        self.clock = clock
        self.limiters = dict(limiters or {})
        self.on_report = on_report
        self._lock = threading.Lock()
        self._paused: set[str] = set()

    # ------------------------------------------------------------- budgets

    def add(self, budget: ApiBudget) -> ApiBudget:
        self.budgets[budget.provider] = budget
        return budget

    def budget(self, provider: str) -> ApiBudget:
        try:
            return self.budgets[provider]
        except KeyError as exc:  # pragma: no cover - configuration error
            raise KeyError(f"no budget configured for {provider!r}") from exc

    # -------------------------------------------------------------- spend

    def can_spend(self, provider: str, calls: int, priority: Priority) -> bool:
        budget = self.budget(provider)
        budget.roll_window(self.clock())
        if provider in self._paused and priority < Priority.REALTIME:
            return False
        cap = int(budget.usable * self.class_share.get(priority, 1.0))
        used_by_class = budget.consumed_by.get(priority, 0)
        return calls <= budget.available and used_by_class + calls <= cap

    def reserve(
        self,
        provider: str,
        calls: int = 1,
        priority: Priority = Priority.INCREMENTAL,
        *,
        note: str = "",
        limiter: str | None = None,
        block: bool = False,
    ) -> bool:
        """Account for ``calls`` before making them.  False means: do not call.

        ``limiter`` names a burst limiter (for instance ``"hubspot_search"``,
        which is 5 requests per second and much tighter than the daily limit).
        """
        with self._lock:
            if not self.can_spend(provider, calls, priority):
                return False
            budget = self.budget(provider)
            budget.consumed += calls
            budget.consumed_by[priority] = budget.consumed_by.get(priority, 0) + calls
            self._record_usage(provider, calls, priority, note)

        if limiter and limiter in self.limiters:
            bucket = self.limiters[limiter]
            if block:
                bucket.acquire(calls)
            elif not bucket.try_acquire(calls):
                return False
        return True

    def spend_or_raise(
        self,
        provider: str,
        calls: int = 1,
        priority: Priority = Priority.INCREMENTAL,
        **kwargs,
    ) -> None:
        if not self.reserve(provider, calls, priority, **kwargs):
            budget = self.budget(provider)
            raise BudgetExhausted(
                f"{provider}: {budget.available} of {budget.usable} usable calls left "
                f"for {priority.name.lower()} (reserve {budget.reserve_fraction:.0%} "
                "is not spendable)",
                retry_after=budget.window_start + timedelta(hours=24) - self.clock(),
            )

    # ------------------------------------------------------------ feedback

    def observe_headers(self, provider: str, headers: Mapping[str, str]) -> UsageSnapshot | None:
        """Adopt the provider's own view of consumption.

        This is how the manager learns about the customer's *other*
        integrations, which our local counter can never see.
        """
        snapshot = parse_headers(provider, headers)
        if snapshot is None:
            return None
        with self._lock:
            budget = self.budget(provider)
            budget.last_snapshot = snapshot
            if snapshot.limit and snapshot.limit != budget.daily_limit:
                budget.daily_limit = snapshot.limit
            budget.external_consumed = max(0, snapshot.used - budget.consumed)
            # If the org is already past our usable line because of somebody
            # else's traffic, stop everything that is not user-facing.
            if snapshot.used >= budget.usable:
                self._paused.add(provider)
            else:
                self._paused.discard(provider)
        return snapshot

    def pause(self, provider: str) -> None:
        self._paused.add(provider)

    def resume(self, provider: str) -> None:
        self._paused.discard(provider)

    def is_paused(self, provider: str) -> bool:
        return provider in self._paused

    # ------------------------------------------------------------- report

    def report(self, provider: str) -> str:
        """Surface consumption to the customer (section 3.3).

        "This sync used 12% of your Salesforce API allocation today" builds
        trust and prevents surprises.
        """
        budget = self.budget(provider)
        ours = budget.consumed / budget.daily_limit if budget.daily_limit else 0
        total = budget.fraction_used
        text = (
            f"{provider}: this integration used {ours:.1%} of the daily API "
            f"allocation ({budget.consumed:,} of {budget.daily_limit:,} calls). "
            f"Total org consumption including other apps: {total:.1%}. "
            f"{budget.available:,} calls remain before the "
            f"{budget.reserve_fraction:.0%} reserve."
        )
        if self.on_report:
            self.on_report(text)
        return text

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        return {
            provider: {
                "daily_limit": b.daily_limit,
                "usable": b.usable,
                "consumed_by_us": b.consumed,
                "consumed_total": b.total_consumed,
                "available": b.available,
                "fraction_used": round(b.fraction_used, 4),
                "paused": provider in self._paused,
            }
            for provider, b in self.budgets.items()
        }

    def _record_usage(self, provider: str, calls: int, priority: Priority, note: str) -> None:
        db = getattr(self, "db", None)
        if db is None:
            return
        from lep.store.db import to_iso

        db.execute(
            "INSERT INTO api_usage (org_id, provider, calls, priority, note, at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.org_id, provider, calls, priority.name, note, to_iso(self.clock())),
        )

    def attach_ledger(self, db) -> None:
        """Optionally persist spend so it is attributable per job."""
        self.db = db
