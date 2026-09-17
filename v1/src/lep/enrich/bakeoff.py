"""Provider bake-off (design.md section 10.2).

Providers disagree -- on employee count, industry, revenue and job title -- for
the same company, routinely and substantially.  So measure them against each
other before committing, with a hand-verified gold set.

Report **per field**, not overall: a provider may be excellent on firmographics
and poor on contact data, which means the right answer is often two providers
with a field-level preference order -- which is exactly what
``SOURCE_PRIORITY`` expresses (section 8.2).

**Cost per correctly-enriched field** is the metric that actually decides.  A
cheaper provider with half the accuracy is more expensive.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from lep.core.types import Record
from lep.enrich.providers import EnrichmentProvider


@dataclass
class FieldScore:
    provider: str
    field: str
    returned: int = 0
    correct: int = 0
    wrong: int = 0
    missing: int = 0

    @property
    def coverage(self) -> float:
        total = self.returned + self.missing
        return 0.0 if total == 0 else self.returned / total

    @property
    def accuracy(self) -> float:
        """Of the values returned, how many were right.

        This is the number that matters, and the one nobody advertises.
        """
        return 0.0 if self.returned == 0 else self.correct / self.returned


@dataclass
class BakeOffReport:
    records: int = 0
    scores: dict[tuple[str, str], FieldScore] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    def by_field(self, field_name: str) -> list[FieldScore]:
        return sorted(
            (s for (p, f), s in self.scores.items() if f == field_name),
            key=lambda s: (-s.accuracy, -s.coverage, s.provider),
        )

    def fields(self) -> list[str]:
        return sorted({f for _, f in self.scores})

    def providers(self) -> list[str]:
        return sorted({p for p, _ in self.scores})

    def cost_per_correct_field(self, provider: str) -> float:
        """The deciding metric."""
        correct = sum(s.correct for (p, _), s in self.scores.items() if p == provider)
        if correct == 0:
            return float("inf")
        return self.cost.get(provider, 0.0) / correct

    def source_ranking(self, field_name: str) -> list[str]:
        """Per-field provider preference, ready to paste into the field policy."""
        return [s.provider for s in self.by_field(field_name) if s.correct]

    def recommended_policy(self) -> dict[str, list[str]]:
        return {f: self.source_ranking(f) for f in self.fields()}

    def render(self) -> str:
        lines = [f"Bake-off over {self.records} records", ""]
        for field_name in self.fields():
            lines.append(f"  {field_name}")
            for score in self.by_field(field_name):
                lines.append(
                    f"    {score.provider:<14} coverage {score.coverage:6.1%}  "
                    f"accuracy {score.accuracy:6.1%}  "
                    f"({score.correct} correct / {score.returned} returned)"
                )
        lines.append("")
        for provider in self.providers():
            per_correct = self.cost_per_correct_field(provider)
            rendered = "n/a" if per_correct == float("inf") else f"{per_correct:.3f}"
            lines.append(
                f"  {provider:<14} {self.calls.get(provider, 0)} calls, "
                f"{self.cost.get(provider, 0):.1f} credits, "
                f"{rendered} credits per correctly-enriched field"
            )
        return "\n".join(lines)


def bake_off(
    records: Sequence[Record],
    providers: Sequence[EnrichmentProvider],
    gold: dict[str, dict[str, Any]],
    *,
    key: str = "email",
) -> BakeOffReport:
    """Send the same records to every provider and compare against a gold set.

    ``gold`` maps a record key to hand-verified field values.  500-1,000
    records is enough to separate providers and small enough to verify by hand,
    which is the only way the gold set is worth anything.
    """
    report = BakeOffReport(records=len(records))
    for record in records:
        truth = gold.get(record.get(key) or record.key, {})
        for provider in providers:
            result = provider.enrich(record)
            report.cost[provider.name] = report.cost.get(provider.name, 0.0) + result.cost_credits
            report.calls[provider.name] = report.calls.get(provider.name, 0) + 1
            for field_name, expected in truth.items():
                score = report.scores.setdefault(
                    (provider.name, field_name),
                    FieldScore(provider.name, field_name),
                )
                got = result.fields.get(field_name)
                if got is None:
                    score.missing += 1
                    continue
                score.returned += 1
                if _equal(got, expected):
                    score.correct += 1
                else:
                    score.wrong += 1
    return report


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        # Employee counts are estimates everywhere; within 20% counts as right.
        larger = max(abs(a), abs(b), 1)
        return abs(a - b) / larger <= 0.2
    return a == b


def disagreement_matrix(
    records: Sequence[Record], providers: Sequence[EnrichmentProvider], field_name: str
) -> dict[tuple[str, str], int]:
    """How often each pair of providers disagrees on one field.

    Useful before you have a gold set: high disagreement means at least one of
    them is wrong a lot, and tells you where hand-verification pays.
    """
    values: dict[str, dict[str, Any]] = defaultdict(dict)
    for record in records:
        for provider in providers:
            result = provider.enrich(record)
            if field_name in result.fields:
                values[record.key][provider.name] = result.fields[field_name]

    counts: dict[tuple[str, str], int] = defaultdict(int)
    names = sorted({p.name for p in providers})
    for per_record in values.values():
        for i, left in enumerate(names):
            for right in names[i + 1 :]:
                if left in per_record and right in per_record:
                    if not _equal(per_record[left], per_record[right]):
                        counts[(left, right)] += 1
    return dict(counts)
