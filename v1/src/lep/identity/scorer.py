"""Probabilistic scoring (design.md section 5.3).

The design document says, correctly, **don't write your own Fellegi-Sunter**:
Splink is mature, calibrated and fast, and the interesting engineering is
everything around it.  :mod:`lep.identity.splink_model` is the adapter that
uses it when it is installed.

This module exists for the case where it is not: a small, readable
Fellegi-Sunter implementation with EM training and term-frequency adjustments,
so the pipeline, the tests and the demo run on the standard library alone.  It
is deliberately the same shape as the Splink settings object, so switching
backends is a constructor change.

Fellegi-Sunter in one paragraph: for each comparison we ask which *level* of
agreement two records reach (exact, close, different, null).  Each level has
two probabilities -- ``m``, the chance of seeing it given the pair is a true
match, and ``u``, the chance given it is not.  The match weight is
``log2(m/u)`` summed across comparisons, added to the prior log odds; the
resulting log odds convert back to a probability.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from lep.core.similarity import jaro_winkler, token_set_ratio
from lep.core.types import Record

Level = int
NULL_LEVEL = -1


@dataclass
class ComparisonLevel:
    name: str
    #: Predicate over the two field values.  Evaluated in order; first hit wins.
    predicate: Callable[[object, object], bool]
    m: float = 0.1
    u: float = 0.1

    @property
    def weight(self) -> float:
        return math.log2(max(self.m, 1e-9) / max(self.u, 1e-9))


@dataclass
class Comparison:
    """One field's comparison levels.

    ``term_frequency_adjustments`` matters more than people expect (section
    5.3).  Two records agreeing on the surname "Nguyen" is weak evidence;
    agreeing on "Featherstonehaugh" is strong.  Without the adjustment both
    count the same, and common-name false merges are a leading cause of the
    super-cluster problem in section 6.2.
    """

    field: str
    levels: list[ComparisonLevel]
    term_frequency_adjustments: bool = False
    #: Populated by :meth:`FellegiSunterScorer.fit` from the corpus.
    term_frequencies: dict[str, float] = field(default_factory=dict)

    def level_for(self, left: Record, right: Record) -> Level:
        a, b = left.get(self.field), right.get(self.field)
        if a in (None, "") or b in (None, ""):
            return NULL_LEVEL
        for index, level in enumerate(self.levels):
            if level.predicate(a, b):
                return index
        return len(self.levels) - 1

    def tf_weight(self, left: Record, right: Record, level: Level) -> float:
        """Extra match weight from how rare the agreeing value is.

        Only applied on the top (exact) level, where "they agree on *this*
        value" is meaningful.  ``u`` for an exact agreement on value ``v`` is
        about ``f(v)^2``; the average ``u`` already in the level is the sum of
        that over all values, so the adjustment is the log ratio between them.
        """
        if not self.term_frequency_adjustments or level != 0:
            return 0.0
        value = left.get(self.field)
        if value is None or value != right.get(self.field):
            return 0.0
        freq = self.term_frequencies.get(str(value))
        if not freq:
            return 0.0
        u_average = max(self.levels[0].u, 1e-9)
        u_term = max(freq * freq, 1e-12)
        return math.log2(u_average / u_term)


def _exact(a: object, b: object) -> bool:
    return a == b


def _jw(threshold: float) -> Callable[[object, object], bool]:
    def compare(a: object, b: object) -> bool:
        return jaro_winkler(str(a), str(b)) >= threshold

    return compare


def _token_set(threshold: float) -> Callable[[object, object], bool]:
    def compare(a: object, b: object) -> bool:
        return token_set_ratio(str(a), str(b)) >= threshold

    return compare


def _always(a: object, b: object) -> bool:
    return True


def _email_local_part(a: object, b: object) -> bool:
    return str(a).split("@")[0] == str(b).split("@")[0]


def name_comparison(field: str, *, tf: bool = True) -> Comparison:
    """Exact / phonetic-or-close / fuzzy / different."""
    return Comparison(
        field=field,
        term_frequency_adjustments=tf,
        levels=[
            ComparisonLevel("exact", _exact, m=0.85, u=0.02),
            ComparisonLevel("very_close", _jw(0.92), m=0.08, u=0.03),
            ComparisonLevel("close", _jw(0.85), m=0.04, u=0.05),
            ComparisonLevel("different", _always, m=0.03, u=0.90),
        ],
    )


def email_comparison(field: str = "email") -> Comparison:
    return Comparison(
        field=field,
        term_frequency_adjustments=True,
        levels=[
            ComparisonLevel("exact", _exact, m=0.70, u=0.001),
            ComparisonLevel("same_local_part", _email_local_part, m=0.10, u=0.005),
            ComparisonLevel("close", _jw(0.93), m=0.05, u=0.01),
            ComparisonLevel("different", _always, m=0.15, u=0.984),
        ],
    )


def company_comparison(field: str = "normalized_company") -> Comparison:
    return Comparison(
        field=field,
        term_frequency_adjustments=True,
        levels=[
            ComparisonLevel("exact", _exact, m=0.70, u=0.02),
            ComparisonLevel("jw_0.9", _jw(0.90), m=0.12, u=0.03),
            ComparisonLevel("token_overlap", _token_set(0.6), m=0.08, u=0.05),
            ComparisonLevel("different", _always, m=0.10, u=0.90),
        ],
    )


def exact_comparison(field: str, *, m: float = 0.60, u: float = 0.001, tf: bool = True) -> Comparison:
    return Comparison(
        field=field,
        term_frequency_adjustments=tf,
        levels=[
            ComparisonLevel("exact", _exact, m=m, u=u),
            ComparisonLevel("different", _always, m=1 - m, u=1 - u),
        ],
    )


def default_comparisons() -> list[Comparison]:
    """The section 5.3 settings, expressed in this implementation."""
    return [
        email_comparison("email"),
        name_comparison("first_name_canonical"),
        name_comparison("last_name"),
        company_comparison("normalized_company"),
        exact_comparison("phone_e164", m=0.45, u=0.0005),
    ]


@dataclass(frozen=True)
class ScoredPair:
    left: str
    right: str
    probability: float
    match_weight: float
    levels: dict[str, str]

    @property
    def pair(self) -> tuple[str, str]:
        return (self.left, self.right) if self.left <= self.right else (self.right, self.left)

    def explain(self) -> str:
        """"Matched on email and surname; differed on first name."

        The cheapest item in the stretch-goal table (section 18) and the one
        that most improves reviewer speed and trust.
        """
        agreed = [f for f, level in self.levels.items() if level in ("exact", "very_close")]
        differed = [f for f, level in self.levels.items() if level == "different"]
        missing = [f for f, level in self.levels.items() if level == "null"]
        parts = []
        if agreed:
            parts.append("matched on " + ", ".join(agreed))
        if differed:
            parts.append("differed on " + ", ".join(differed))
        if missing:
            parts.append("no data for " + ", ".join(missing))
        return "; ".join(parts) or "no comparable fields"


class FellegiSunterScorer:
    """Probabilistic record linkage scorer.

    ``prior`` is the expected proportion of candidate pairs that are true
    matches.  It matters: with a million candidate pairs and a thousand true
    duplicates, the prior log odds are about -10 bits, and a model that ignores
    that will happily call everything a match.
    """

    def __init__(
        self,
        comparisons: Sequence[Comparison] | None = None,
        *,
        prior: float = 1e-4,
    ) -> None:
        self.comparisons = list(comparisons or default_comparisons())
        self.prior = prior
        self.trained = False
        self.training_report: dict[str, object] = {}

    # ------------------------------------------------------------ scoring

    @property
    def prior_weight(self) -> float:
        return math.log2(self.prior / (1 - self.prior))

    def pattern(self, left: Record, right: Record) -> tuple[Level, ...]:
        return tuple(c.level_for(left, right) for c in self.comparisons)

    def score(self, left: Record, right: Record) -> ScoredPair:
        weight = self.prior_weight
        levels: dict[str, str] = {}
        for comparison in self.comparisons:
            level = comparison.level_for(left, right)
            if level == NULL_LEVEL:
                # A null contributes nothing: absence of data is not evidence
                # either way, and treating it as disagreement is a classic way
                # to suppress true matches on sparse records.
                levels[comparison.field] = "null"
                continue
            weight += comparison.levels[level].weight
            weight += comparison.tf_weight(left, right, level)
            levels[comparison.field] = comparison.levels[level].name
        return ScoredPair(
            left=left.key,
            right=right.key,
            probability=_weight_to_probability(weight),
            match_weight=weight,
            levels=levels,
        )

    def score_pairs(
        self, records: Sequence[Record], pairs: Iterable[tuple[str, str]]
    ) -> list[ScoredPair]:
        index = {r.key: r for r in records}
        out = []
        for a, b in pairs:
            left, right = index.get(a), index.get(b)
            if left is None or right is None:
                continue
            out.append(self.score(left, right))
        return out

    # ----------------------------------------------------------- training

    def fit_term_frequencies(self, records: Sequence[Record]) -> None:
        """Relative frequency of each value, per TF-adjusted comparison."""
        for comparison in self.comparisons:
            if not comparison.term_frequency_adjustments:
                continue
            counts = Counter(
                str(r.get(comparison.field))
                for r in records
                if r.get(comparison.field) not in (None, "")
            )
            total = sum(counts.values())
            if total:
                comparison.term_frequencies = {
                    value: count / total for value, count in counts.items()
                }

    def fit_supervised(
        self,
        records: Sequence[Record],
        labelled: Iterable[tuple[str, str, bool]],
        *,
        smoothing: float = 1.0,
    ) -> dict[str, object]:
        """Estimate ``m`` from labelled matches and ``u`` from labelled non-matches.

        Preferred over EM when labels exist, and by the time auto-merge is on
        the table they do: every review decision is a label (section 5.4).
        """
        self.fit_term_frequencies(records)
        index = {r.key: r for r in records}
        match_counts = [Counter() for _ in self.comparisons]
        nonmatch_counts = [Counter() for _ in self.comparisons]
        n_match = n_nonmatch = 0

        for a, b, is_match in labelled:
            left, right = index.get(a), index.get(b)
            if left is None or right is None:
                continue
            if is_match:
                n_match += 1
            else:
                n_nonmatch += 1
            for i, comparison in enumerate(self.comparisons):
                level = comparison.level_for(left, right)
                if level == NULL_LEVEL:
                    continue
                (match_counts if is_match else nonmatch_counts)[i][level] += 1

        for i, comparison in enumerate(self.comparisons):
            _assign(comparison, match_counts[i], nonmatch_counts[i], smoothing)

        if n_match + n_nonmatch:
            self.prior = max(1e-6, min(0.5, n_match / (n_match + n_nonmatch)))
        self.trained = True
        self.training_report = {
            "method": "supervised",
            "matches": n_match,
            "non_matches": n_nonmatch,
            "prior": self.prior,
        }
        return self.training_report

    def fit_em(
        self,
        records: Sequence[Record],
        pairs: Sequence[tuple[str, str]],
        *,
        iterations: int = 20,
        tolerance: float = 1e-6,
        smoothing: float = 1.0,
    ) -> dict[str, object]:
        """Unsupervised EM over the comparison vectors of candidate pairs.

        The standard Fellegi-Sunter estimator, assuming conditional
        independence between comparisons given match status.  That assumption
        is wrong in detail (first name and email local part are correlated),
        which is one reason the resulting probabilities must be *calibrated
        against review decisions* before any of them gates an auto-merge.
        """
        self.fit_term_frequencies(records)
        index = {r.key: r for r in records}
        patterns = Counter(
            self.pattern(index[a], index[b])
            for a, b in pairs
            if a in index and b in index
        )
        if not patterns:
            return {"method": "em", "pairs": 0}

        lam = max(self.prior, 1e-4)
        log_likelihood = None
        for iteration in range(iterations):
            # E step: responsibility of the match class for each pattern.
            responsibilities: dict[tuple[Level, ...], float] = {}
            total_ll = 0.0
            for pattern, count in patterns.items():
                pm = lam * self._pattern_probability(pattern, "m")
                pu = (1 - lam) * self._pattern_probability(pattern, "u")
                denom = pm + pu
                responsibilities[pattern] = 0.0 if denom == 0 else pm / denom
                total_ll += count * math.log(max(denom, 1e-300))

            # M step.
            match_counts = [defaultdict(float) for _ in self.comparisons]
            nonmatch_counts = [defaultdict(float) for _ in self.comparisons]
            weighted_matches = 0.0
            total = 0.0
            for pattern, count in patterns.items():
                g = responsibilities[pattern]
                weighted_matches += g * count
                total += count
                for i, level in enumerate(pattern):
                    if level == NULL_LEVEL:
                        continue
                    match_counts[i][level] += g * count
                    nonmatch_counts[i][level] += (1 - g) * count

            for i, comparison in enumerate(self.comparisons):
                _assign(
                    comparison,
                    Counter(match_counts[i]),
                    Counter(nonmatch_counts[i]),
                    smoothing,
                )
            lam = max(1e-6, min(0.5, weighted_matches / total))

            if log_likelihood is not None and abs(total_ll - log_likelihood) < tolerance:
                break
            log_likelihood = total_ll

        self.prior = lam
        self.trained = True
        self.training_report = {
            "method": "em",
            "pairs": sum(patterns.values()),
            "distinct_patterns": len(patterns),
            "iterations": iteration + 1,
            "prior": lam,
            "log_likelihood": log_likelihood,
        }
        return self.training_report

    def _pattern_probability(self, pattern: tuple[Level, ...], which: str) -> float:
        p = 1.0
        for i, level in enumerate(pattern):
            if level == NULL_LEVEL:
                continue
            comparison_level = self.comparisons[i].levels[level]
            p *= comparison_level.m if which == "m" else comparison_level.u
        return p

    # -------------------------------------------------------- diagnostics

    def weights_table(self) -> list[tuple[str, str, float, float, float]]:
        """``(field, level, m, u, weight)`` -- read this after every retrain."""
        rows = []
        for comparison in self.comparisons:
            for level in comparison.levels:
                rows.append(
                    (comparison.field, level.name, level.m, level.u, level.weight)
                )
        return rows


def _assign(
    comparison: Comparison,
    match_counts: Counter,
    nonmatch_counts: Counter,
    smoothing: float,
) -> None:
    n_levels = len(comparison.levels)
    m_total = sum(match_counts.values()) + smoothing * n_levels
    u_total = sum(nonmatch_counts.values()) + smoothing * n_levels
    for index, level in enumerate(comparison.levels):
        level.m = (match_counts.get(index, 0) + smoothing) / m_total
        level.u = (nonmatch_counts.get(index, 0) + smoothing) / u_total


def _weight_to_probability(weight: float) -> float:
    # Guard against overflow at the extremes; a weight of +/- 60 bits is
    # already a probability of 1 or 0 to within floating point.
    weight = max(-200.0, min(200.0, weight))
    odds = 2.0**weight
    return odds / (1.0 + odds)
