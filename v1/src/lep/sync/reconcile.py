"""Polling reconciliation (design.md section 7.3).

Push is lower latency and cheaper in API calls.  **Run polling reconciliation
anyway** -- every push mechanism drops events, and a drifted record that nobody
notices for a month is worse than a slightly stale one.

Poll on ``SystemModstamp``, not ``LastModifiedDate``: ``SystemModstamp`` also
advances on system-level changes that the other misses.

Log the discrepancy rate.  **A rising rate means the push path is broken**, and
it is the only signal that says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from lep.budget.manager import BudgetManager, Priority
from lep.core.types import CrmEvent, SourceKind, hash_value, utcnow
from lep.sync.engine import SyncEngine


@dataclass
class ReconciliationReport:
    source: str
    since: datetime
    records_checked: int = 0
    fields_checked: int = 0
    discrepancies: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    repaired: int = 0
    calls: int = 0

    @property
    def discrepancy_rate(self) -> float:
        if self.fields_checked == 0:
            return 0.0
        return len(self.discrepancies) / self.fields_checked

    def summary(self) -> str:
        return (
            f"{self.source}: checked {self.records_checked} records / "
            f"{self.fields_checked} fields since {self.since.isoformat()}; "
            f"{len(self.discrepancies)} discrepancies "
            f"({self.discrepancy_rate:.4%}); {self.repaired} repaired"
        )

    def alert_if_rising(self, previous_rate: float, *, factor: float = 2.0) -> str | None:
        if previous_rate > 0 and self.discrepancy_rate > previous_rate * factor:
            return (
                f"{self.source}: reconciliation discrepancy rate rose from "
                f"{previous_rate:.4%} to {self.discrepancy_rate:.4%} -- the push "
                "path is dropping events"
            )
        return None


def reconcile(
    engine: SyncEngine,
    source: str,
    since: datetime,
    *,
    budget: BudgetManager | None = None,
    repair: bool = True,
) -> ReconciliationReport:
    """Compare our projection of a CRM against what it actually holds.

    Anything that differs is fed back through ``ingest`` as a synthetic event,
    which means it goes through the same idempotency, ordering and policy path
    as a webhook would.  Reconciliation is a *source of truth check*, not a
    second write path -- keeping it that way is what stops it becoming its own
    class of bug.
    """
    client = engine.clients[source]
    report = ReconciliationReport(source=source, since=since)

    if budget is not None and not budget.reserve(
        source, 1, Priority.INCREMENTAL, note="reconcile"
    ):
        return report

    for record_id, fields in client.updated_since(since):
        report.records_checked += 1
        projected = engine.remote_state.get((source, record_id), {})
        for remote_field, value in fields.items():
            report.fields_checked += 1
            known = projected.get(remote_field)
            if known is not None and hash_value(known) == hash_value(value):
                continue
            report.discrepancies.append((record_id, remote_field, known, value))
            if not repair:
                continue
            engine.ingest(
                CrmEvent(
                    source=source,
                    record_id=record_id,
                    field=remote_field,
                    new_value=value,
                    observed_at=client.records[record_id].system_modstamp,
                    change_id=f"reconcile:{record_id}:{remote_field}:{hash_value(value)}",
                    source_kind=SourceKind.CRM,
                )
            )
            report.repaired += 1

    report.calls = 1
    if budget is not None:
        budget.observe_headers(source, client.headers())
    return report


class Reconciler:
    """Scheduled reconciliation with discrepancy-rate history."""

    def __init__(
        self,
        engine: SyncEngine,
        *,
        interval: timedelta = timedelta(hours=1),
        budget: BudgetManager | None = None,
    ) -> None:
        self.engine = engine
        self.interval = interval
        self.budget = budget
        self.last_run: dict[str, datetime] = {}
        self.rates: dict[str, list[float]] = {}
        self.alerts: list[str] = []

    def run(self, source: str, *, now: datetime | None = None) -> ReconciliationReport:
        now = now or utcnow()
        since = self.last_run.get(source, now - self.interval)
        report = reconcile(self.engine, source, since, budget=self.budget)
        history = self.rates.setdefault(source, [])
        if history:
            alert = report.alert_if_rising(history[-1])
            if alert:
                self.alerts.append(alert)
        history.append(report.discrepancy_rate)
        self.last_run[source] = now
        return report
