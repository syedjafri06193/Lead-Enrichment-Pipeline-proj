"""Test harness (design.md section 17).

A simulated clock and a two-CRM environment, so the soak test in section 17.1
can run twenty-four simulated hours in a few milliseconds.

The clock matters more than it looks.  Echo suppression, the write-log window
and the loop circuit breaker are all *time-relative*, so a test that fires a
thousand edits at wall-clock speed is not testing echo suppression -- it is
testing the circuit breaker, which correctly trips on a thousand changes to one
record in a second.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from lep.budget.manager import ApiBudget, BudgetManager
from lep.cluster.components import EdgeIndex
from lep.conflict.log import ConflictLog
from lep.conflict.strategies import PolicySet
from lep.core.types import CrmEvent, Observation, Record, SourceKind
from lep.identity.normalize import normalize_record
from lep.pipeline import find_policy_path
from lep.review.queue import ReviewQueue
from lep.store.db import Database
from lep.store.entities import EntityStore
from lep.store.observations import ObservationStore
from lep.sync.circuit import LoopDetector
from lep.sync.echo import WriteLog
from lep.sync.engine import SyncEngine
from lep.sync.hubspot import FakeHubSpot
from lep.sync.salesforce import FakeSalesforce

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SimulatedClock:
    """A clock the test drives."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


class SyncTestEnv:
    """Two CRMs, one engine, and a clock that moves when you say so."""

    def __init__(
        self,
        *,
        salesforce: FakeSalesforce | None = None,
        hubspot: FakeHubSpot | None = None,
        policies: PolicySet | None = None,
        seed: int = 7,
        step: timedelta = timedelta(seconds=90),
        attribution_loss_rate: float = 0.35,
        drop_webhook_rate: float = 0.0,
    ) -> None:
        self.clock = SimulatedClock()
        self.step = step
        self.rng = random.Random(seed)
        self.queue: list[CrmEvent] = []
        self.db = Database()
        self.observations = ObservationStore(self.db)
        self.entities = EntityStore(self.db)
        self.policies = policies or PolicySet.load(find_policy_path())
        self.alerts: list[str] = []

        self.salesforce = salesforce or FakeSalesforce(
            on_event=self.queue.append,
            attribution_loss_rate=attribution_loss_rate,
            drop_webhook_rate=drop_webhook_rate,
            rng=random.Random(seed + 1),
            clock=self.clock,
        )
        self.hubspot = hubspot or FakeHubSpot(
            on_event=self.queue.append,
            attribution_loss_rate=attribution_loss_rate,
            drop_webhook_rate=drop_webhook_rate,
            rng=random.Random(seed + 2),
            clock=self.clock,
        )
        for client in (self.salesforce, self.hubspot):
            client.on_event = self.queue.append
            client.clock = self.clock

        self.budget = BudgetManager(
            "test-org",
            [
                ApiBudget("test-org", "salesforce", 1_000_000),
                ApiBudget("test-org", "hubspot", 1_000_000),
            ],
            clock=self.clock,
        )
        self.review = ReviewQueue(self.db)
        self.engine = SyncEngine(
            observations=self.observations,
            entities=self.entities,
            policies=self.policies,
            clients={"salesforce": self.salesforce, "hubspot": self.hubspot},
            write_log=WriteLog(self.db, clock=self.clock),
            loop_detector=LoopDetector(
                self.db, alert=self.alerts.append, clock=self.clock
            ),
            budget=self.budget,
            conflict_log=ConflictLog(self.db),
            review_queue=self.review,
        )
        self.bidirectional_fields: list[str] = []
        self.entity_id = self._seed_contact()
        self._edit_seq = 0

    # ----------------------------------------------------------- set-up

    def _seed_contact(self) -> str:
        entity = self.entities.create("person")
        self.salesforce.seed(
            "003AAA", {"Email": "ann@acme.com", "FirstName": "Ann", "LastName": "Lee"}
        )
        self.hubspot.seed(
            "101", {"email": "ann@acme.com", "firstname": "Ann", "lastname": "Lee"}
        )
        self.entities.link(entity.id, "salesforce", "003AAA")
        self.entities.link(entity.id, "hubspot", "101")
        self.engine.remote_state[("salesforce", "003AAA")] = dict(
            self.salesforce.records["003AAA"].fields
        )
        self.engine.remote_state[("hubspot", "101")] = dict(
            self.hubspot.records["101"].fields
        )
        return entity.id

    def enable_bidirectional(self, fields: Sequence[str]) -> None:
        self.bidirectional_fields = list(fields)

    # ------------------------------------------------------------ edits

    def edit_random_side(self) -> None:
        """One human edit on one side, then time moves on."""
        self._edit_seq += 1
        field_name = self.rng.choice(self.bidirectional_fields or ["first_name"])
        value = f"{field_name[:2]}-{self._edit_seq}"
        if self.rng.random() < 0.5:
            remote = {"first_name": "FirstName", "last_name": "LastName", "phone": "Phone"}[
                field_name
            ]
            self.salesforce.user_edit("003AAA", remote, value)
        else:
            remote = {"first_name": "firstname", "last_name": "lastname", "phone": "phone"}[
                field_name
            ]
            self.hubspot.user_edit("101", remote, value)
        self.clock.advance(self.step)

    def run_sync_cycle(self, max_rounds: int = 10) -> int:
        """Drain the webhook queue, including anything our own writes produce."""
        writes = 0
        for _ in range(max_rounds):
            if not self.queue:
                break
            batch, self.queue[:] = list(self.queue), []
            for event in batch:
                self.engine.ingest(event)
            writes += self.engine.flush()
            self.clock.advance(timedelta(seconds=1))
        return writes

    # ---------------------------------------------------------- readback

    def total_writes(self) -> int:
        return self.engine.stats.writes

    def total_user_edits(self) -> int:
        return self.engine.stats.user_edits

    def loop_breaker_trips(self) -> int:
        return self.engine.loop_detector.trips

    def resolved(self, field_name: str):
        from lep.store.resolve import resolve

        return resolve(
            self.observations,
            self.entity_id,
            field_name,
            self.policies.for_field(field_name),
            at=self.clock(),
        )


# --------------------------------------------------------------------------
# Fixtures for the matching tests
# --------------------------------------------------------------------------


def build_chain(n: int = 8, edge_score: float = 0.85) -> EdgeIndex:
    """A ← 0.85 → B ← 0.85 → C ... with the ends clearly distinct.

    Cohesion of a chain of n is about 2/n, which is what the guard catches.
    """
    edges = EdgeIndex()
    for i in range(n - 1):
        edges.add(f"r{i}", f"r{i + 1}", edge_score)
    # The ends were compared and found distinct -- evidence, not absence of it.
    edges.add("r0", f"r{n - 1}", 0.05)
    return edges


def chain_records(n: int = 8) -> list[Record]:
    """Records that a naive matcher chains together: same surname, same company."""
    first_names = ["J", "John", "Jon", "Johnny", "Jonathan", "Jonny", "Joh", "Jhon"]
    companies = ["Acme", "Acme Corp", "Acme Corporation", "Acme Inc"]
    out = []
    for i in range(n):
        out.append(
            Record(
                source="salesforce",
                source_record_id=f"r{i}",
                fields={
                    "first_name": first_names[i % len(first_names)],
                    "last_name": "Smith",
                    "company": companies[i % len(companies)],
                    "email": f"jsmith{i}@acme.com",
                },
            )
        )
    for record in out:
        normalize_record(record)
    return out


@dataclass
class SeededEntity:
    """An entity present in every store, for the erasure test."""

    id: str
    email: str
    links: list[tuple[str, str]] = field(default_factory=list)


def seed_entity_everywhere(pipeline: Any) -> SeededEntity:
    """Put one person into every store the erasure has to reach."""
    entity = pipeline.entities.create("person")
    email = "erase.me@acme.com"
    now = pipeline.clock()

    for source, record_id in (("salesforce", "003ERASE"), ("hubspot", "999")):
        pipeline.entities.link(entity.id, source, record_id, confidence=1.0)
        pipeline.observations.append(
            Observation(
                entity_id=entity.id,
                field="email",
                value=email,
                source=source,
                source_record_id=record_id,
                observed_at=now,
            )
        )
        client = pipeline.clients.get(source)
        if client is not None:
            client.seed(record_id, {"email": email})

    pipeline.observations.append(
        Observation(
            entity_id=entity.id,
            field="job_title",
            value="VP Engineering",
            source="enrichment",
            source_kind=SourceKind.ENRICHMENT,
            observed_at=now,
            confidence=0.7,
        )
    )
    # A conflict, a review item and a dead-lettered event all referencing them.
    pipeline.observations.append(
        Observation(
            entity_id=entity.id,
            field="job_title",
            value="Head of Engineering",
            source="hubspot",
            observed_at=now,
        )
    )
    pipeline.resolved(entity.id)
    pipeline.review.enqueue(
        "pair", entity.id, right_key="hubspot:999", score=0.9, reason="seeded"
    )
    pipeline.dlq.send(
        "salesforce:003ERASE:1", {"entity_id": entity.id, "email": email}, "boom", 5
    )
    other = pipeline.entities.create("person")
    pipeline.entities.merge(entity.id, other.id, score=0.999, decided_by="test")

    return SeededEntity(
        id=entity.id,
        email=email,
        links=[("salesforce", "003ERASE"), ("hubspot", "999")],
    )


def labelled_pairs(records: Sequence[Record], truth: dict[str, str]) -> list[tuple[str, str, bool]]:
    """Every pair, labelled by whether the two records are the same person."""
    out = []
    for i, left in enumerate(records):
        for right in records[i + 1 :]:
            out.append((left.key, right.key, truth.get(left.key) == truth.get(right.key)))
    return out


def true_pairs(records: Sequence[Record], truth: dict[str, str]) -> list[tuple[str, str]]:
    return [
        tuple(sorted((a, b)))
        for a, b, is_match in labelled_pairs(records, truth)
        if is_match
    ]


def synthetic_corpus(
    n_people: int = 60, duplicates_per_person: int = 2, seed: int = 11
) -> tuple[list[Record], dict[str, str]]:
    """A small corpus with known duplicates, for measuring precision and recall.

    Deliberately includes the hard cases from section 17.4: nicknames, married
    names, transliterations, same name at a different company, and the same
    person at a different email.
    """
    rng = random.Random(seed)
    firsts = [
        ("william", ["bill", "will", "willie"]),
        ("robert", ["bob", "rob", "bobby"]),
        ("katherine", ["kate", "katie", "kathy"]),
        ("jose", ["josé", "jose"]),
        ("nguyen", ["nguyen"]),
        ("michael", ["mike", "mick"]),
    ]
    lasts = ["smith", "nguyen", "garcia", "okafor", "muller", "featherstonehaugh"]
    companies = ["Acme Corp", "Globex Inc", "Initech", "Umbrella Ltd"]

    records: list[Record] = []
    truth: dict[str, str] = {}
    seq = 0
    for person in range(n_people):
        formal, nicknames = firsts[person % len(firsts)]
        last = lasts[person % len(lasts)]
        company = companies[person % len(companies)]
        person_id = f"p{person}"
        for copy in range(1 + rng.randint(1, duplicates_per_person)):
            seq += 1
            first = formal if copy == 0 else rng.choice(nicknames)
            email = (
                f"{formal}.{last}{person}@{company.split()[0].lower()}.com"
                if copy == 0
                else f"{first[0]}{last}{person}@{company.split()[0].lower()}.com"
            )
            source = "salesforce" if copy % 2 == 0 else "hubspot"
            record = Record(
                source=source,
                source_record_id=f"{person_id}-{seq}",
                fields={
                    "first_name": first,
                    "last_name": last if copy < 2 else last.upper(),
                    "email": email,
                    "company": company if copy == 0 else company.split()[0],
                    "phone": f"+1415555{1000 + person:04d}",
                },
            )
            normalize_record(record)
            records.append(record)
            truth[record.key] = person_id
    return records, truth
