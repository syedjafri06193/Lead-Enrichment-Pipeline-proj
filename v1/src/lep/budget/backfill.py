"""Backfill as its own system (design.md section 12.2).

Backfill is the dominant consumer of the budget and a different workload from
incremental sync: everything rather than deltas, Bulk rather than REST, days
rather than continuous, resume-from-checkpoint rather than retry-the-event, and
the lowest priority of anything running.

A backfill that must finish in one window is a backfill that will exhaust the
customer's allocation.  A backfill that cannot be paused is one that will take
down their integrations at quarter end.  So this runs as long as it needs to,
checkpoints every batch, and stops the moment real-time work needs headroom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator, Sequence

from lep.budget.manager import BudgetManager, Priority, plan_reads
from lep.core.types import utcnow
from lep.store.db import Database, to_iso


@dataclass
class BackfillState:
    name: str
    position: str = ""
    processed: int = 0
    calls_spent: int = 0
    batches: int = 0
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    paused_reason: str | None = None

    @property
    def is_paused(self) -> bool:
        return self.paused_reason is not None


class Checkpoints:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, name: str) -> str | None:
        row = self.db.one("SELECT position FROM checkpoints WHERE name = ?", (name,))
        return row["position"] if row else None

    def set(self, name: str, position: str) -> None:
        self.db.execute(
            "INSERT INTO checkpoints (name, position, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET position = excluded.position,"
            " updated_at = excluded.updated_at",
            (name, position, to_iso(utcnow())),
        )

    def clear(self, name: str) -> None:
        self.db.execute("DELETE FROM checkpoints WHERE name = ?", (name,))


class Backfill:
    """Resumable, throttled, pausable bulk load.

    ``fetch`` is called with ``(cursor, limit)`` and returns
    ``(records, next_cursor)``; ``next_cursor`` of None ends the run.
    """

    def __init__(
        self,
        name: str,
        provider: str,
        budget: BudgetManager,
        checkpoints: Checkpoints,
        fetch: Callable[[str | None, int], tuple[Sequence[object], str | None]],
        *,
        batch_size: int = 10_000,
        daily_fraction: float = 0.20,
        priority: Priority = Priority.BACKFILL,
    ) -> None:
        self.name = name
        self.provider = provider
        self.budget = budget
        self.checkpoints = checkpoints
        self.fetch = fetch
        self.batch_size = batch_size
        #: Hard ceiling on this job's share of the daily allocation.
        self.daily_fraction = daily_fraction
        self.priority = priority
        self.state = BackfillState(name=name, position=checkpoints.get(name) or "")

    @property
    def call_budget_for_run(self) -> int:
        return int(self.budget.budget(self.provider).daily_limit * self.daily_fraction)

    def run(self, max_batches: int | None = None) -> Iterator[Sequence[object]]:
        """Yield batches until the budget says stop, or the source is drained.

        Stopping is not a failure.  The checkpoint means the next run picks up
        exactly where this one left off, which is why a multi-day backfill
        against a Developer org's 15,000-call cap is a normal outcome rather
        than an incident.
        """
        cursor = self.state.position or None
        self.state.paused_reason = None
        spent_this_run = 0
        batches = 0

        while True:
            if max_batches is not None and batches >= max_batches:
                self.state.paused_reason = "batch limit for this run reached"
                return
            if self.budget.is_paused(self.provider):
                self.state.paused_reason = "budget manager paused this provider"
                return

            plan = plan_reads(self.batch_size)
            if spent_this_run + plan.calls > self.call_budget_for_run:
                self.state.paused_reason = (
                    f"run has used its {self.daily_fraction:.0%} share of the "
                    f"{self.provider} daily allocation"
                )
                return
            if not self.budget.reserve(
                self.provider,
                plan.calls,
                self.priority,
                note=f"backfill:{self.name}",
            ):
                self.state.paused_reason = (
                    "insufficient budget at backfill priority -- real-time work has "
                    "the headroom"
                )
                return

            records, next_cursor = self.fetch(cursor, self.batch_size)
            spent_this_run += plan.calls
            batches += 1
            self.state.calls_spent += plan.calls
            self.state.batches += 1
            self.state.processed += len(records)

            if records:
                yield records

            if next_cursor is None:
                self.state.position = ""
                self.state.finished_at = utcnow()
                self.checkpoints.clear(self.name)
                return

            cursor = next_cursor
            self.state.position = next_cursor
            self.checkpoints.set(self.name, next_cursor)

    def resume(self, max_batches: int | None = None) -> Iterator[Sequence[object]]:
        self.state.position = self.checkpoints.get(self.name) or ""
        return self.run(max_batches=max_batches)
