"""The precision bar (design.md section 5.4).

A false merge and a false split are not comparable errors:

===============  ================================  ====================
                 False merge                       False split
===============  ================================  ====================
Effect           two people become one record      one person, two rows
Reversibility    effectively irreversible in CRMs  trivially fixable
Data loss        one record's values are gone      none
Privacy          A's data sits in B's record       none
Detection        often never noticed               obvious
===============  ================================  ====================

At 99% precision -- a number most practitioners would call excellent -- merging
100,000 candidate pairs destroys **1,000 records**.  So the threshold structure
is banded, not binary, and the auto-merge band must be justified by precision
*measured on adjudicated decisions from your own data* (section 17.2), never by
a benchmark.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from lep.core.types import Decision


@dataclass(frozen=True)
class MatchPolicy:
    """Banded thresholds.

    ``auto_merge_threshold`` defaults to 0.995 *and to being disabled*:
    ``auto_merge_enabled`` starts False so that everything routes through
    review until :func:`lep.review.queue.ReviewQueue.precision_by_band` says
    otherwise.  Build the review queue before auto-merge, measure, then promote
    a band.
    """

    auto_merge_threshold: float = 0.995
    review_threshold: float = 0.80
    auto_merge_enabled: bool = False
    #: Minimum number of adjudicated decisions in a band before it may be
    #: promoted to automatic.
    min_decisions_to_promote: int = 500
    #: Measured precision the band must clear to be promoted.
    required_precision: float = 0.999

    def __post_init__(self) -> None:
        if not 0 < self.review_threshold <= self.auto_merge_threshold <= 1:
            raise ValueError(
                "thresholds must satisfy 0 < review <= auto_merge <= 1"
            )


def decide(score: float, policy: MatchPolicy = MatchPolicy()) -> Decision:
    """Map a calibrated match probability onto an action."""
    if score >= policy.auto_merge_threshold:
        return Decision.AUTO_MERGE if policy.auto_merge_enabled else Decision.REVIEW
    if score >= policy.review_threshold:
        return Decision.REVIEW
    return Decision.DISTINCT


def score_band(score: float, width: float = 0.05) -> str:
    """Bucket label used when reporting precision by band."""
    if score >= 0.999:
        return "0.999+"
    if score >= 0.995:
        return "0.995-0.999"
    if score >= 0.99:
        return "0.990-0.995"
    # Integer arithmetic: (0.90 // 0.05) * 0.05 is 0.8500000000000001 in
    # binary floating point, which would put a score in the band below its own.
    index = math.floor(round(score / width, 9))
    lower = max(0.0, min(0.99 - width, index * width))
    return f"{lower:.2f}-{lower + width:.2f}"


def destroyed_records(precision: float, merges: int) -> float:
    """The arithmetic from section 5.4, as a function you can call.

    ``destroyed_records(0.99, 100_000) == 1000.0``.  Put it in the admin UI
    next to the threshold slider.
    """
    return (1.0 - precision) * merges


def precision_at_fixed_recall(
    scored: Sequence[tuple[float, bool]], target_recall: float
) -> tuple[float, float]:
    """Return ``(threshold, precision)`` at the lowest threshold hitting recall.

    Tune for precision at a fixed recall, never F1 (section 5.4): F1 treats a
    false merge and a false split as the same kind of mistake, which is exactly
    the error the whole design is arranged to avoid.
    """
    ordered = sorted(scored, key=lambda item: -item[0])
    total_positives = sum(1 for _, label in ordered if label)
    if total_positives == 0:
        return (1.0, 0.0)
    true_positives = 0
    predicted = 0
    for score, label in ordered:
        predicted += 1
        if label:
            true_positives += 1
        recall = true_positives / total_positives
        if recall >= target_recall:
            return (score, true_positives / predicted)
    return (ordered[-1][0], total_positives / len(ordered))


def f1(precision: float, recall: float) -> float:
    """Provided only so the tests can demonstrate why it is the wrong metric."""
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def promote_band(
    band: str,
    precision: float,
    decisions: int,
    policy: MatchPolicy,
) -> tuple[bool, str]:
    """Should this score band be promoted from review to auto-merge?

    Both conditions have to hold: enough adjudicated decisions to make the
    estimate meaningful, and a measured precision above the bar.  "The model
    scored these 0.998" is not evidence; "reviewers agreed with 1,200 of 1,200
    in this band" is.
    """
    if decisions < policy.min_decisions_to_promote:
        return (
            False,
            f"{band}: only {decisions} adjudicated decisions, need "
            f"{policy.min_decisions_to_promote}",
        )
    if precision < policy.required_precision:
        wrong = destroyed_records(precision, 100_000)
        return (
            False,
            f"{band}: measured precision {precision:.5f} is below "
            f"{policy.required_precision:.5f} -- that is {wrong:,.0f} destroyed "
            "records per 100,000 merges",
        )
    return (True, f"{band}: precision {precision:.5f} over {decisions} decisions")


def bands(scores: Iterable[float]) -> dict[str, int]:
    out: dict[str, int] = {}
    for score in scores:
        band = score_band(score)
        out[band] = out.get(band, 0) + 1
    return dict(sorted(out.items()))
