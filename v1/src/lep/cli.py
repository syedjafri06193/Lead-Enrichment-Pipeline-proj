"""Command line entry point: ``lep demo``, ``lep policy``, ``lep bakeoff``.

The demo runs the whole thing end to end against fake CRMs and prints what each
stage decided, including the things it deliberately refused to decide.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from lep.budget.manager import plan_reads
from lep.conflict.strategies import PolicySet
from lep.core.types import Record
from lep.enrich.bakeoff import bake_off
from lep.enrich.providers import StaticProvider, enrich_entity
from lep.identity.blocking import measure_recall
from lep.identity.policy import MatchPolicy, destroyed_records
from lep.pipeline import Pipeline, find_policy_path
from lep.privacy.erasure import erase
from lep.sync.hubspot import FakeHubSpot
from lep.sync.reconcile import reconcile
from lep.sync.salesforce import FakeSalesforce
from lep.testing import (
    EPOCH,
    SimulatedClock,
    SyncTestEnv,
    seed_entity_everywhere,
    synthetic_corpus,
    true_pairs,
)


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * len(title))


def cmd_policy(args: argparse.Namespace) -> int:
    policies = PolicySet.load(args.path or find_policy_path())
    problems = policies.validate()
    rule("Field policy")
    for object_type in sorted(policies.objects):
        print(f"  {object_type}")
        for name in policies.fields(object_type):
            policy = policies.for_field(name, object_type)
            ttl = f"{policy.ttl_days}d" if policy.ttl_days else "-"
            print(
                f"    {name:<18} master={str(policy.master):<12} "
                f"direction={policy.direction.value:<18} "
                f"strategy={policy.strategy.value:<16} ttl={ttl}"
            )
    print()
    print(f"  written to salesforce: {policies.synced_fields('salesforce')}")
    print(f"  written to hubspot:    {policies.synced_fields('hubspot')}")
    if problems:
        print("\n  PROBLEMS:")
        for problem in problems:
            print(f"    - {problem}")
        return 1
    print("\n  policy validates")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    clock = SimulatedClock()
    events: list = []
    clients = {
        "salesforce": FakeSalesforce(on_event=events.append, clock=clock),
        "hubspot": FakeHubSpot(on_event=events.append, clock=clock),
    }
    alerts: list[str] = []
    pipeline = Pipeline(clients=clients, clock=clock, alert=alerts.append)

    # ---------------------------------------------------------------- M2
    rule("1. The API budget you don't own (section 3)")
    print("  Salesforce Developer org daily cap: 15,000")
    print(f"  usable after the 30% reserve:       {pipeline.budget.budget('salesforce').usable:,}")
    plan = plan_reads(500_000)
    print(f"  500,000-record backfill:            {plan.note}")
    pipeline.budget.observe_headers(
        "salesforce", {"Sforce-Limit-Info": "api-usage=6000/15000"}
    )
    print(f"  after reading the provider header:  {pipeline.budget.report('salesforce')}")

    # ------------------------------------------------------------- M4/M5
    rule("2. Matching: deterministic, blocked, scored, banded (section 5)")
    records, truth = synthetic_corpus(n_people=args.people)
    recall = measure_recall(records, true_pairs(records, truth))
    print(f"  blocking: {recall.summary()}")
    outcome = pipeline.match(records, train=True, labels=[
        (a, b, truth[a] == truth[b])
        for a, b in sorted({tuple(sorted((r1.key, r2.key)))
                            for r1 in records[:20] for r2 in records[:20] if r1.key < r2.key})
    ])
    print(f"  {outcome.summary()}")
    print(
        f"  auto-merge is {'ON' if pipeline.match_policy.auto_merge_enabled else 'OFF'}: "
        "everything routes through review until precision is measured"
    )
    print(
        f"  at 99% precision, 100,000 auto-merges would destroy "
        f"{destroyed_records(0.99, 100_000):,.0f} records"
    )

    # ---------------------------------------------------------------- M7
    rule("3. Clustering with guards (section 6)")
    clusters = outcome.clusters
    assert clusters is not None
    print(f"  cluster sizes:     {clusters.size_distribution()}")
    print(f"  largest cluster:   {clusters.largest}")
    print(f"  split by guards:   {clusters.split_count}")
    print(f"  sent to review:    {len(clusters.review)}")

    # ---------------------------------------------------------------- M6
    rule("4. The review queue, before auto-merge (sections 5.4, 17.2)")
    for item in pipeline.review.pending(limit=3):
        detail = item.payload.get("explanation") or item.reason
        print(f"  [{item.score if item.score is not None else 'n/a':>8}] {item.left_key} ~ {item.right_key}: {detail}")
    print(f"  queue depth: {pipeline.review.depth()}")
    for line in pipeline.review.promotion_report(MatchPolicy()):
        print(f"  {line}")

    # ---------------------------------------------------------------- M9
    rule("5. Bidirectional sync, 24 simulated hours (section 7)")
    env = SyncTestEnv()
    env.enable_bidirectional(["first_name", "phone"])
    for _ in range(args.edits):
        env.edit_random_side()
        env.run_sync_cycle()
    print(f"  user edits:          {env.total_user_edits()}")
    print(f"  writes made:         {env.total_writes()}")
    print(f"  echoes suppressed:   {env.engine.echo.stats.suppressed} "
          f"(origin tag {env.engine.echo.stats.by_actor}, "
          f"write log {env.engine.echo.stats.by_write_log})")
    print(f"  circuit breaker:     {env.loop_breaker_trips()} trips")
    print(f"  resolved first_name: {env.resolved('first_name').value} "
          f"({env.resolved('first_name').reason})")

    dropped = SyncTestEnv(drop_webhook_rate=1.0)
    dropped.salesforce.user_edit("003AAA", "FirstName", "Annabel")
    dropped.run_sync_cycle()
    report = reconcile(dropped.engine, "salesforce", EPOCH - timedelta(days=1))
    print(f"  reconciliation:      {report.summary()}")

    # --------------------------------------------------------- M10 / M11
    rule("6. Conflicts and enrichment (sections 8, 10)")
    entity = pipeline.entities.create("person").id
    record = Record("salesforce", "003DEMO", {"email": "ann@acme.com"})
    pipeline.entities.link(entity, "salesforce", "003DEMO")
    providers = [
        StaticProvider("provider_a", {"ann@acme.com": {"job_title": "VP Engineering"}},
                       cost_per_call=2.0, confidence=0.9),
        StaticProvider("provider_b", {"ann@acme.com": {"job_title": "Engineering Manager"}},
                       cost_per_call=0.5, confidence=0.5),
    ]
    enrich_entity(pipeline.observations, entity, record, providers)
    clients["salesforce"].user_edit("003DEMO", "Title", "Head of Platform")
    for event in list(events):
        pipeline.engine.ingest(event)
    events.clear()
    resolved = pipeline.resolved(entity)["job_title"]
    print(f"  job_title resolves to: {resolved.value!r} ({resolved.reason})")
    print(f"  conflicting fields:    {pipeline.conflicts.top_conflicting_fields(3)}")

    gold = {"ann@acme.com": {"job_title": "VP Engineering"}}
    print()
    print(bake_off([record], providers, gold).render())

    # --------------------------------------------------------------- M12
    rule("7. Erasure, with evidence (section 11)")
    seeded = seed_entity_everywhere(pipeline)
    report = erase(
        seeded.id,
        "gdpr_request",
        db=pipeline.db,
        stores=[pipeline.observations, pipeline.conflicts, pipeline.dlq],
        entity_store=pipeline.entities,
        crm_clients=pipeline.clients,
        identifiers=[seeded.email],
    )
    print(report.render())

    rule("Health")
    print(json.dumps(pipeline.health(), indent=2, default=str))
    if alerts:
        rule("Alerts raised")
        for alert in alerts:
            print(f"  {alert}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    pipeline = Pipeline()
    print(json.dumps(pipeline.health(), indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lep", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the whole pipeline against fake CRMs")
    demo.add_argument("--people", type=int, default=40)
    demo.add_argument("--edits", type=int, default=200)
    demo.set_defaults(func=cmd_demo)

    policy = sub.add_parser("policy", help="print and validate the field policy")
    policy.add_argument("--path", default=None)
    policy.set_defaults(func=cmd_policy)

    health = sub.add_parser("health", help="print the health snapshot")
    health.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
