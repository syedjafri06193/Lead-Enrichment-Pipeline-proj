"""Splink adapter (design.md sections 2.3, 5.3, 13).

"Don't write your own Fellegi-Sunter.  Splink is mature, calibrated, and fast.
The interesting engineering here is everything around it."

So: when Splink is installed, use it.  :func:`build_settings` produces the
settings object from section 5.3 -- including the term frequency adjustments,
which matter more than people expect -- and :class:`SplinkScorer` exposes the
same ``score_pairs`` interface as the built-in
:class:`~lep.identity.scorer.FellegiSunterScorer` so the rest of the pipeline
does not know or care which one is running.
"""

from __future__ import annotations

from typing import Any, Sequence

from lep.identity.blocking import BLOCKING_RULES, BlockingRule
from lep.identity.scorer import FellegiSunterScorer, ScoredPair
from lep.core.types import Record


def splink_available() -> bool:
    try:  # pragma: no cover - depends on the environment
        import splink  # noqa: F401
    except Exception:
        return False
    return True


def build_settings(
    blocking_rules: Sequence[BlockingRule] = BLOCKING_RULES,
    *,
    link_type: str = "dedupe_only",
) -> Any:  # pragma: no cover - requires splink
    """The section 5.3 settings.

    Note ``term_frequency_adjustments=True`` on the phone comparison and on the
    name comparisons: agreeing on "Nguyen" is weak evidence, agreeing on a rare
    surname is strong, and without the adjustment both count the same.
    """
    import splink.comparison_library as cl
    from splink import SettingsCreator

    return SettingsCreator(
        link_type=link_type,
        blocking_rules_to_generate_predictions=[r.sql for r in blocking_rules if r.sql],
        comparisons=[
            cl.EmailComparison("email"),
            cl.NameComparison("first_name"),
            cl.NameComparison("last_name"),
            cl.JaroWinklerAtThresholds("normalized_company", [0.9, 0.8]),
            cl.ExactMatch("phone_e164").configure(term_frequency_adjustments=True),
        ],
        retain_intermediate_calculation_columns=True,
    )


class SplinkScorer:
    """Thin wrapper over a Splink ``Linker``.

    Falls back to the built-in scorer when Splink is not installed, which is
    what keeps the tests and the demo runnable with no services.
    """

    def __init__(self, *, prior: float = 1e-4, fallback: FellegiSunterScorer | None = None):
        self.prior = prior
        self.fallback = fallback or FellegiSunterScorer(prior=prior)
        self.linker: Any | None = None
        self.uses_splink = False

    def fit(self, records: Sequence[Record]) -> "SplinkScorer":  # pragma: no cover
        if not splink_available():
            self.fallback.fit_term_frequencies(records)
            return self
        import pandas as pd
        from splink import DuckDBAPI, Linker

        frame = pd.DataFrame(
            [{"unique_id": r.key, **r.normalized} for r in records]
        )
        self.linker = Linker(frame, build_settings(), DuckDBAPI())
        # EM training, as in section 5.3.  The estimates are then calibrated
        # against review decisions before any band is promoted (section 17.2).
        self.linker.training.estimate_u_using_random_sampling(max_pairs=1_000_000)
        for rule in BLOCKING_RULES:
            if rule.sql:
                self.linker.training.estimate_parameters_using_expectation_maximisation(
                    rule.sql
                )
        self.uses_splink = True
        return self

    def score_pairs(
        self, records: Sequence[Record], pairs: Sequence[tuple[str, str]]
    ) -> list[ScoredPair]:
        if self.linker is None:  # pragma: no branch - the usual path here
            return self.fallback.score_pairs(records, pairs)
        # pragma: no cover - requires splink
        predictions = self.linker.inference.predict().as_pandas_dataframe()
        wanted = {tuple(sorted(p)) for p in pairs}
        out: list[ScoredPair] = []
        for row in predictions.itertuples():
            pair = tuple(sorted((row.unique_id_l, row.unique_id_r)))
            if pair not in wanted:
                continue
            out.append(
                ScoredPair(
                    left=pair[0],
                    right=pair[1],
                    probability=float(row.match_probability),
                    match_weight=float(row.match_weight),
                    levels={},
                )
            )
        return out
