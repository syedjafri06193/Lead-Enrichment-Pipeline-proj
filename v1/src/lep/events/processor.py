"""Event processing loop (design.md section 9).

Ties together the three mechanics that make at-least-once delivery survivable:
a claim so duplicates are skipped, bounded retries with backoff, and a DLQ with
depth alerting for the events that will never succeed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from lep.core.types import CrmEvent
from lep.events.dlq import DeadLetterQueue
from lep.events.idempotency import IdempotencyStore
from lep.store.db import Database

MAX_ATTEMPTS = 5


def backoff(attempt: int, *, base: float = 1.0, cap: float = 300.0, jitter: bool = True) -> float:
    """Exponential backoff with full jitter.

    Jitter matters when a provider outage ends: without it every queued event
    retries at the same instant and the recovery looks exactly like a DDoS of
    the customer's API allocation.
    """
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0, delay) if jitter else delay


@dataclass
class ProcessorStats:
    processed: int = 0
    duplicates: int = 0
    retried: int = 0
    dead_lettered: int = 0
    failed_keys: dict[str, int] = field(default_factory=dict)


class EventProcessor:
    """Runs a handler over events, exactly-once-ish and never forever."""

    def __init__(
        self,
        db: Database,
        handler: Callable[[CrmEvent], None],
        *,
        dlq: DeadLetterQueue | None = None,
        idempotency: IdempotencyStore | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        retry: Callable[[CrmEvent, float], None] | None = None,
        claim_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.db = db
        self.handler = handler
        self.dlq = dlq or DeadLetterQueue(db)
        self.idempotency = idempotency or IdempotencyStore(db, ttl=claim_ttl)
        self.max_attempts = max_attempts
        self.retry = retry
        self.stats = ProcessorStats()

    def handle(self, event: CrmEvent) -> bool:
        """Process one event.  Returns True when the handler ran."""
        key = event.idempotency_key
        if not self.idempotency.claim(key):
            self.stats.duplicates += 1
            return False
        return self._run(event, key)

    def _run(self, event: CrmEvent, key: str) -> bool:
        try:
            self.handler(event)
        except Exception as exc:  # noqa: BLE001 - the point is to catch everything
            attempts = self._increment_attempts(key)
            # The claim is released so a genuine retry is not mistaken for a
            # duplicate; the attempt counter is what bounds the loop.
            self.idempotency.release(key)
            if attempts >= self.max_attempts:
                self.dlq.send(key, _payload(event), str(exc), attempts)
                self.stats.dead_lettered += 1
                return False
            self.stats.retried += 1
            if self.retry is not None:
                self.retry(event, backoff(attempts))
            return False
        self.stats.processed += 1
        return True

    def _increment_attempts(self, key: str) -> int:
        self.db.execute(
            "INSERT INTO event_attempts (key, attempts) VALUES (?, 1)"
            " ON CONFLICT (key) DO UPDATE SET attempts = attempts + 1",
            (key,),
        )
        attempts = int(
            self.db.scalar("SELECT attempts FROM event_attempts WHERE key = ?", (key,)) or 1
        )
        self.stats.failed_keys[key] = attempts
        return attempts


def _payload(event: CrmEvent) -> dict[str, object]:
    return {
        "source": event.source,
        "record_id": event.record_id,
        "field": event.field,
        "new_value": event.new_value,
        "observed_at": event.observed_at.isoformat(),
        "change_id": event.change_id,
    }
