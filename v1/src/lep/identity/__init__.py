"""Identity resolution (design.md section 5)."""

from lep.identity.blocking import (
    BLOCKING_RULES,
    BlockingRule,
    BlockingStats,
    candidate_pairs,
    measure_recall,
)
from lep.identity.deterministic import DeterministicMatch, deterministic_matches
from lep.identity.normalize import (
    double_metaphone,
    normalize_company,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_record,
)
from lep.identity.policy import MatchPolicy, decide
from lep.identity.scorer import (
    Comparison,
    FellegiSunterScorer,
    ScoredPair,
    default_comparisons,
)

__all__ = [
    "BLOCKING_RULES",
    "BlockingRule",
    "BlockingStats",
    "Comparison",
    "DeterministicMatch",
    "FellegiSunterScorer",
    "MatchPolicy",
    "ScoredPair",
    "candidate_pairs",
    "decide",
    "default_comparisons",
    "deterministic_matches",
    "double_metaphone",
    "measure_recall",
    "normalize_company",
    "normalize_email",
    "normalize_name",
    "normalize_phone",
    "normalize_record",
]
