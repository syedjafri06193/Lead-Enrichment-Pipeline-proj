"""The API budget you don't own (design.md section 3)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lep.budget.backfill import Backfill, Checkpoints
from lep.budget.manager import (
    ApiBudget,
    BudgetExhausted,
    BudgetManager,
    Priority,
    plan_reads,
)
from lep.budget.providers import (
    BurstLimiter,
    hubspot_daily_limit,
    parse_hubspot_headers,
    parse_sforce_limit_info,
    salesforce_daily_limit,
)
from lep.testing import SimulatedClock


def test_documented_limits():
    assert salesforce_daily_limit("enterprise", licenses=50) == 150_000
    assert salesforce_daily_limit("unlimited", licenses=10) == 150_000
    assert salesforce_daily_limit("developer", licenses=500) == 15_000  # hard cap
    assert hubspot_daily_limit("professional") == 625_000
    assert hubspot_daily_limit("enterprise") == 1_000_000


def test_reserve_is_never_spent():
    budget = ApiBudget("org", "salesforce", 10_000, reserve_fraction=0.30)
    assert budget.usable == 7_000
    budget.consumed = 7_000
    assert budget.available == 0


def test_backfill_cannot_starve_real_time_work():
    manager = BudgetManager("org", [ApiBudget("org", "salesforce", 10_000)])
    spent = sum(
        1 for _ in range(10_000) if manager.reserve("salesforce", 1, Priority.BACKFILL)
    )
    assert spent < 7_000, "backfill must not be able to take the whole usable budget"
    assert manager.reserve("salesforce", 1, Priority.REALTIME)


def test_consumption_is_read_from_the_provider():
    """Our own counter misses every other integration on the account."""
    manager = BudgetManager("org", [ApiBudget("org", "salesforce", 15_000)])
    manager.reserve("salesforce", 10, Priority.INCREMENTAL)
    snapshot = manager.observe_headers(
        "salesforce", {"Sforce-Limit-Info": "api-usage=9000/15000"}
    )
    assert snapshot.used == 9_000
    budget = manager.budget("salesforce")
    assert budget.total_consumed == 9_000  # not our local 10
    assert budget.external_consumed == 8_990


def test_other_apps_exhausting_the_org_pauses_background_work():
    manager = BudgetManager("org", [ApiBudget("org", "hubspot", 625_000)])
    manager.observe_headers(
        "hubspot",
        {
            "X-HubSpot-RateLimit-Daily": "625000",
            "X-HubSpot-RateLimit-Daily-Remaining": "10000",
        },
    )
    assert manager.is_paused("hubspot")
    assert not manager.reserve("hubspot", 1, Priority.BACKFILL)


def test_header_parsing():
    assert parse_sforce_limit_info({"Sforce-Limit-Info": "api-usage=18/5000"}).used == 18
    assert parse_sforce_limit_info({"other": "x"}) is None
    snapshot = parse_hubspot_headers(
        {
            "X-HubSpot-RateLimit-Daily": "250000",
            "X-HubSpot-RateLimit-Daily-Remaining": "249000",
            "X-HubSpot-RateLimit-Remaining": "95",
        }
    )
    assert (snapshot.used, snapshot.remaining_burst) == (1_000, 95)


def test_search_burst_limiter_is_five_per_second():
    """Much tighter than the general limit, and matching is when you need it."""
    now = {"t": 0.0}
    limiter = BurstLimiter.hubspot_search(clock=lambda: now["t"])
    assert sum(1 for _ in range(10) if limiter.try_acquire()) == 5
    now["t"] = 1.0
    assert sum(1 for _ in range(10) if limiter.try_acquire()) == 5


def test_plan_reads_matches_the_section_3_4_arithmetic():
    assert plan_reads(500_000).strategy == "bulk"
    assert plan_reads(500_000).jobs == 50
    assert plan_reads(500).calls == 3          # batched at 200
    assert plan_reads(2_000).strategy == "rest_batched"
    assert plan_reads(2_001).strategy == "bulk"


def test_report_is_customer_facing():
    manager = BudgetManager("org", [ApiBudget("org", "salesforce", 100_000)])
    manager.reserve("salesforce", 12_000, Priority.BACKFILL)
    text = manager.report("salesforce")
    assert "12.0%" in text and "reserve" in text


def test_spend_or_raise():
    manager = BudgetManager("org", [ApiBudget("org", "salesforce", 100)])
    with pytest.raises(BudgetExhausted):
        manager.spend_or_raise("salesforce", 1_000, Priority.REALTIME)


def test_rolling_window_resets_after_24h():
    clock = SimulatedClock()
    budget = ApiBudget("org", "salesforce", 1_000, window_start=clock())
    budget.consumed = 700
    assert not budget.roll_window(clock())
    clock.advance(timedelta(hours=25))
    assert budget.roll_window(clock())
    assert budget.consumed == 0


def test_backfill_is_resumable_and_pausable(db):
    """M2 exit criterion: a simulated backfill against a Developer org's
    15,000-call cap completes over multiple days without exhausting it."""
    clock = SimulatedClock()
    manager = BudgetManager(
        "org", [ApiBudget("org", "salesforce", 15_000, window_start=clock())], clock=clock
    )
    checkpoints = Checkpoints(db)
    total_records = 500_000
    fetched = {"n": 0}

    def fetch(cursor, limit):
        start = int(cursor or 0)
        end = min(start + limit, total_records)
        fetched["n"] += end - start
        return ([{"id": i} for i in range(start, end)], str(end) if end < total_records else None)

    days = 0
    while days < 40:
        # REST-batched at 200, capped at 5% of the day's allocation: the shape
        # of a backfill that has to coexist with a customer's other apps.
        backfill = Backfill(
            "initial",
            "salesforce",
            manager,
            checkpoints,
            fetch,
            batch_size=200,
            daily_fraction=0.05,
        )
        list(backfill.resume())
        budget = manager.budget("salesforce")
        assert budget.total_consumed <= budget.usable, "never eats the reserve"
        if backfill.state.finished_at:
            break
        # Next day: the rolling window resets and the checkpoint is picked up.
        clock.advance(timedelta(hours=25))
        budget.roll_window(clock())
        days += 1

    assert fetched["n"] == total_records
    assert days >= 1, "a 500k REST backfill on a Developer org spans multiple days"
    assert checkpoints.get("initial") is None
    # And the reason to reach for Bulk: two orders of magnitude fewer calls.
    assert plan_reads(total_records).calls < plan_reads(200).calls * (total_records / 200)


def test_backfill_pauses_when_budget_is_paused(db):
    manager = BudgetManager("org", [ApiBudget("org", "salesforce", 15_000)])
    manager.pause("salesforce")
    backfill = Backfill(
        "b", "salesforce", manager, Checkpoints(db), lambda c, n: ([], None)
    )
    assert list(backfill.run()) == []
    assert "paused" in (backfill.state.paused_reason or "")
