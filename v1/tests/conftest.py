from __future__ import annotations

import pytest

from lep.pipeline import Pipeline
from lep.store.db import Database
from lep.store.entities import EntityStore
from lep.store.observations import ObservationStore
from lep.testing import SimulatedClock, SyncTestEnv
from lep.sync.hubspot import FakeHubSpot
from lep.sync.salesforce import FakeSalesforce


@pytest.fixture
def db() -> Database:
    return Database()


@pytest.fixture
def observations(db: Database) -> ObservationStore:
    return ObservationStore(db)


@pytest.fixture
def entities(db: Database) -> EntityStore:
    return EntityStore(db)


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock()


@pytest.fixture
def pipeline(clock: SimulatedClock) -> Pipeline:
    events: list = []
    clients = {
        "salesforce": FakeSalesforce(on_event=events.append, clock=clock),
        "hubspot": FakeHubSpot(on_event=events.append, clock=clock),
    }
    pipe = Pipeline(clients=clients, clock=clock)
    pipe.events = events  # type: ignore[attr-defined]
    return pipe


@pytest.fixture
def env() -> SyncTestEnv:
    return SyncTestEnv()
