"""Normalization, blocking, scoring and the precision bar (sections 5, 16, 17)."""

from __future__ import annotations

import pytest

from lep.core.types import Record
from lep.identity.blocking import (
    BLOCKING_RULES,
    BlockingRule,
    BlockingStats,
    candidate_pairs,
    measure_recall,
    rule_contribution,
)
from lep.identity.deterministic import deterministic_matches
from lep.identity.normalize import (
    canonical_first_name,
    double_metaphone,
    normalize_company,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_record,
    phonetically_equal,
)
from lep.identity.policy import (
    MatchPolicy,
    decide,
    destroyed_records,
    f1,
    precision_at_fixed_recall,
    promote_band,
    score_band,
)
from lep.identity.scorer import FellegiSunterScorer
from lep.core.types import Decision
from lep.testing import labelled_pairs, synthetic_corpus, true_pairs


# --------------------------------------------------------------- normalize


def test_gmail_ignores_dots_and_other_providers_do_not():
    """The asymmetry that produces a class of false merges (section 16.1)."""
    assert normalize_email("J.Smith+news@Gmail.com") == "jsmith@gmail.com"
    assert normalize_email("jsmith@googlemail.com") == "jsmith@gmail.com"
    # Dots preserved off Gmail: these may be two different people.
    assert normalize_email("j.smith@company.com") == "j.smith@company.com"
    assert normalize_email("jsmith@company.com") != normalize_email("j.smith@company.com")
    # +tag stripping is broadly safe.
    assert normalize_email("jsmith+billing@company.com") == "jsmith@company.com"


@pytest.mark.parametrize(
    "value",
    ["J.Smith+x@Gmail.com", "  BOB@Example.COM ", "no-at-sign", "a@b.co"],
)
def test_normalization_is_idempotent(value):
    """Property test (section 17.3)."""
    once = normalize_email(value)
    assert normalize_email(once or "") == once
    assert normalize_name(normalize_name(value)) == normalize_name(value)
    assert normalize_company(normalize_company(value)) == normalize_company(value)


def test_phone_normalization_refuses_to_guess():
    assert normalize_phone("+1 (415) 555-0100 x22") == "+14155550100"
    assert normalize_phone("415.555.0100") == "+14155550100"
    assert normalize_phone("00 44 20 7123 4567") == "+442071234567"
    assert normalize_phone("123") is None      # better None than plausible-but-wrong


def test_company_suffix_variation():
    assert normalize_company("Alphabet Inc.") == normalize_company("Alphabet")
    assert normalize_company("The Acme Corporation") == "acme"
    # And the case string normalization cannot fix:
    assert normalize_company("Alphabet") != normalize_company("Google")


@pytest.mark.parametrize(
    "a,b",
    [("Smith", "Smyth"), ("Schmidt", "Schmitt"), ("Catherine", "Katherine"),
     ("Mueller", "Muller"), ("Philips", "Filips"), ("Thompson", "Tompson")],
)
def test_double_metaphone_agrees_on_variants(a, b):
    assert phonetically_equal(a, b)


@pytest.mark.parametrize("a,b", [("Smith", "Jones"), ("Anderson", "Henderson")])
def test_double_metaphone_separates_distinct_names(a, b):
    assert not phonetically_equal(a, b)


def test_double_metaphone_handles_non_english_names():
    """The reason for Double Metaphone over Soundex (section 5.2)."""
    for name in ("Nguyen", "Okafor", "Kowalczyk", "Sánchez", "Björk"):
        primary, _ = double_metaphone(name)
        assert primary, f"no code produced for {name}"
    assert phonetically_equal("Sánchez", "Sanchez")


def test_nicknames_are_evidence_not_a_rule():
    assert canonical_first_name("Bill") == "william"
    assert canonical_first_name("Bob") == "robert"
    # Ambiguous nicknames are left alone rather than guessed.
    assert canonical_first_name("Alex") == "alex"


# ------------------------------------------------------------ deterministic


def test_role_addresses_are_not_identities():
    records = [
        Record("salesforce", "1", {"email": "info@acme.com"}),
        Record("hubspot", "2", {"email": "info@acme.com"}),
        Record("salesforce", "3", {"email": "ann@acme.com"}),
        Record("hubspot", "4", {"email": "ann@acme.com"}),
    ]
    for record in records:
        normalize_record(record)
    matched = {m.pair for m in deterministic_matches(records)}
    assert ("hubspot:4", "salesforce:3") in matched
    assert ("hubspot:2", "salesforce:1") not in matched


def test_unverified_phone_does_not_create_a_match():
    records = [
        Record("salesforce", "1", {"phone": "+14155550100", "last_name": "Smith"}),
        Record("hubspot", "2", {"phone": "+14155550100", "last_name": "Smith"}),
    ]
    for record in records:
        normalize_record(record)
    assert deterministic_matches(records) == []

    for record in records:
        record.fields["phone_verified"] = True
        normalize_record(record)
    assert len(deterministic_matches(records)) == 1


# ---------------------------------------------------------------- blocking


def test_blocking_keeps_recall_high_and_pairs_low():
    records, truth = synthetic_corpus(n_people=40)
    stats = measure_recall(records, true_pairs(records, truth))
    assert stats.recall >= 0.95, f"blocking drops true pairs: {stats.summary()}"
    assert stats.reduction_ratio > 0.8, "blocking is not reducing enough"


def test_exploding_blocks_are_skipped_and_reported():
    """Blocking on a free-mail domain alone puts everybody in one block."""
    records = [
        Record("salesforce", str(i), {"email": f"user{i}@gmail.com", "last_name": "Smith"})
        for i in range(60)
    ]
    for record in records:
        normalize_record(record)
    rule = BlockingRule("domain_only", lambda r: r.get("email_domain"))
    stats = BlockingStats()
    pairs = candidate_pairs(records, [rule], max_block_size=50, stats=stats)
    assert pairs == set()
    assert stats.oversized_blocks
    assert stats.skipped_pairs == 60 * 59 // 2


def test_every_blocking_rule_earns_its_place():
    records, truth = synthetic_corpus(n_people=40)
    contribution = rule_contribution(records, true_pairs(records, truth), BLOCKING_RULES)
    assert set(contribution) == {r.name for r in BLOCKING_RULES}
    assert all(value >= 0 for value in contribution.values())


# ------------------------------------------------------------------ scorer


def test_training_separates_matches_from_non_matches():
    records, truth = synthetic_corpus(n_people=40)
    labels = labelled_pairs(records, truth)
    pairs = [(a, b) for a, b, _ in labels]

    scorer = FellegiSunterScorer(prior=0.01)
    scorer.fit_supervised(records, labels)
    scored = {p.pair: p.probability for p in scorer.score_pairs(records, pairs)}

    match_scores = [scored[tuple(sorted((a, b)))] for a, b, m in labels if m]
    non_match_scores = [scored[tuple(sorted((a, b)))] for a, b, m in labels if not m]
    assert min(match_scores) > max(non_match_scores) * 0.5
    assert sum(match_scores) / len(match_scores) > 0.9
    assert sum(non_match_scores) / len(non_match_scores) < 0.1


def test_em_training_runs_unsupervised():
    """EM with no labels at all, as a cold start before any review decisions.

    The estimated prior is capped at 0.5: a blocked candidate set is heavily
    enriched with true duplicates, and a model that believes most pairs match
    is one merge threshold away from a catastrophe.
    """
    records, truth = synthetic_corpus(n_people=30)
    pairs = sorted(candidate_pairs(records))
    scorer = FellegiSunterScorer(prior=0.05)
    report = scorer.fit_em(records, pairs, iterations=10)
    assert report["method"] == "em"
    assert 0 < report["prior"] <= 0.5
    assert scorer.trained

    scored = {p.pair: p.probability for p in scorer.score_pairs(records, pairs)}
    matches = [s for pair, s in scored.items() if truth[pair[0]] == truth[pair[1]]]
    non_matches = [s for pair, s in scored.items() if truth[pair[0]] != truth[pair[1]]]
    assert sum(matches) / len(matches) > sum(non_matches) / max(1, len(non_matches))


def test_term_frequency_adjustment_discounts_common_surnames():
    """Agreeing on "Nguyen" is weak evidence; a rare surname is strong."""
    common = [
        Record("salesforce", f"c{i}", {"last_name": "Nguyen", "first_name": "Minh"})
        for i in range(50)
    ]
    rare = [
        Record("salesforce", f"r{i}", {"last_name": "Featherstonehaugh", "first_name": "Minh"})
        for i in range(2)
    ]
    records = common + rare
    for record in records:
        normalize_record(record)

    scorer = FellegiSunterScorer(prior=0.01)
    scorer.fit_term_frequencies(records)
    common_pair = scorer.score(records[0], records[1])
    rare_pair = scorer.score(rare[0], rare[1])
    assert rare_pair.match_weight > common_pair.match_weight


def test_nulls_are_not_treated_as_disagreement():
    sparse = Record("salesforce", "1", {"last_name": "Okafor"})
    full = Record("hubspot", "2", {"last_name": "Okafor", "email": "n@acme.com"})
    for record in (sparse, full):
        normalize_record(record)
    scorer = FellegiSunterScorer(prior=0.1)
    pair = scorer.score(sparse, full)
    assert pair.levels["email"] == "null"
    assert "no data for" in pair.explain()


def test_explanations_are_readable():
    left = Record("salesforce", "1", {"email": "a@acme.com", "first_name": "Ann", "last_name": "Lee"})
    right = Record("hubspot", "2", {"email": "a@acme.com", "first_name": "Anna", "last_name": "Lee"})
    for record in (left, right):
        normalize_record(record)
    explanation = FellegiSunterScorer().score(left, right).explain()
    assert "matched on" in explanation


# ------------------------------------------------------------------ policy


def test_the_arithmetic_that_governs_everything():
    """99% precision over 100,000 merges destroys 1,000 records."""
    assert destroyed_records(0.99, 100_000) == pytest.approx(1_000)


def test_auto_merge_is_off_until_measured():
    assert decide(0.9999) is Decision.REVIEW
    assert decide(0.9999, MatchPolicy(auto_merge_enabled=True)) is Decision.AUTO_MERGE
    assert decide(0.85) is Decision.REVIEW
    assert decide(0.5) is Decision.DISTINCT


def test_a_band_is_promoted_only_on_measured_precision():
    policy = MatchPolicy()
    ok, why = promote_band("0.995-0.999", 0.998, 5_000, policy)
    assert not ok and "destroyed records" in why
    ok, why = promote_band("0.999+", 0.9995, 5_000, policy)
    assert ok
    ok, why = promote_band("0.999+", 1.0, 10, policy)
    assert not ok and "adjudicated decisions" in why


def test_tune_precision_at_fixed_recall_not_f1():
    scored = [(0.99, True), (0.97, True), (0.90, False), (0.85, True), (0.4, False)]
    threshold, precision = precision_at_fixed_recall(scored, target_recall=0.66)
    assert threshold == pytest.approx(0.97)
    assert precision == 1.0
    # F1 would happily trade a merge error for a split error; this is the
    # number the design says never to optimise.
    assert f1(0.5, 1.0) > f1(1.0, 0.33)


def test_score_bands_are_fine_grained_at_the_top():
    assert score_band(0.9995) == "0.999+"
    assert score_band(0.996) == "0.995-0.999"
    assert score_band(0.83) == "0.80-0.85"


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError):
        MatchPolicy(auto_merge_threshold=0.5, review_threshold=0.9)
