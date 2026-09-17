"""Event-driven mechanics (design.md section 9)."""

from lep.events.dlq import DeadLetterQueue, DlqEntry
from lep.events.idempotency import IdempotencyStore
from lep.events.ordering import OrderingResult, apply_observation
from lep.events.processor import EventProcessor, backoff

__all__ = [
    "DeadLetterQueue",
    "DlqEntry",
    "EventProcessor",
    "IdempotencyStore",
    "OrderingResult",
    "apply_observation",
    "backoff",
]
