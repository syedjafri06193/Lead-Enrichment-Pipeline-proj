"""Deterministic matching (design.md section 5.1).

Cheap, high-precision rules run before any probabilistic work, and they resolve
most of the volume.  Probabilistic matching handles the remainder.

Every rule here carries its own precision claim, and two carry explicit
exclusions that exist because the obvious version of the rule is wrong:

* role-address emails (``info@``, ``sales@``) are not people, so an exact email
  match on one is not an identity;
* a shared free-mail *domain* is not evidence of anything, which is why the
  email rule keys on the whole address and never on the domain.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from lep.core.types import Record

#: Local parts that identify a function, not a person.
ROLE_LOCALPARTS = {
    "info", "sales", "support", "admin", "contact", "hello", "help", "billing",
    "accounts", "office", "team", "marketing", "press", "careers", "jobs",
    "noreply", "no-reply", "webmaster", "postmaster", "enquiries", "inquiries",
}


@dataclass(frozen=True)
class DeterministicRule:
    name: str
    key: Callable[[Record], str | None]
    #: Confidence attached to links this rule creates.  These are the numbers
    #: that let a deterministic link be distinguished from a probabilistic one
    #: later, when somebody asks why two records were joined.
    confidence: float
    description: str


def _email_key(record: Record) -> str | None:
    email = record.get("email")
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0]
    if local in ROLE_LOCALPARTS:
        return None
    return f"email:{email}"


def _external_id_key(record: Record) -> str | None:
    external = record.fields.get("external_id")
    if not external:
        return None
    return f"extid:{record.source}:{external}"


def _phone_lastname_key(record: Record) -> str | None:
    phone = record.get("phone_e164")
    last = record.get("last_name")
    verified = record.fields.get("phone_verified", False)
    if not phone or not last or not verified:
        return None
    return f"phone+last:{phone}:{last}"


def _linkedin_key(record: Record) -> str | None:
    slug = record.get("linkedin")
    return f"linkedin:{slug}" if slug else None


#: Ordered by precision, highest first.
DETERMINISTIC_RULES: tuple[DeterministicRule, ...] = (
    DeterministicRule(
        "same_external_id",
        _external_id_key,
        1.0,
        "Same external id from the same source: certain.",
    ),
    DeterministicRule(
        "normalized_email",
        _email_key,
        0.99,
        "Normalized email exact match, excluding role addresses: very high.",
    ),
    DeterministicRule(
        "linkedin_url",
        _linkedin_key,
        0.97,
        "Same LinkedIn profile: high.",
    ),
    DeterministicRule(
        "verified_phone_and_last_name",
        _phone_lastname_key,
        0.95,
        "Verified phone plus surname: high. Unverified phone is not used -- "
        "shared office and household numbers make it a false-merge source.",
    ),
)


@dataclass(frozen=True)
class DeterministicMatch:
    left: str
    right: str
    rule: str
    confidence: float

    @property
    def pair(self) -> tuple[str, str]:
        return (self.left, self.right) if self.left <= self.right else (self.right, self.left)


def deterministic_matches(
    records: Sequence[Record],
    rules: Iterable[DeterministicRule] = DETERMINISTIC_RULES,
) -> list[DeterministicMatch]:
    """All pairs joined by at least one deterministic rule.

    Records are keyed by ``source:source_record_id``.  Only the
    highest-confidence rule is reported for each pair.
    """
    best: dict[tuple[str, str], DeterministicMatch] = {}
    for rule in rules:
        buckets: dict[str, list[Record]] = defaultdict(list)
        for record in records:
            key = rule.key(record)
            if key:
                buckets[key].append(record)
        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    match = DeterministicMatch(
                        bucket[i].key, bucket[j].key, rule.name, rule.confidence
                    )
                    existing = best.get(match.pair)
                    if existing is None or match.confidence > existing.confidence:
                        best[match.pair] = match
    return sorted(best.values(), key=lambda m: (-m.confidence, m.pair))


def deterministic_key_index(
    records: Sequence[Record],
    rules: Iterable[DeterministicRule] = DETERMINISTIC_RULES,
) -> dict[str, list[str]]:
    """``{rule key: [record keys]}`` -- useful for debugging a surprise merge."""
    index: dict[str, list[str]] = defaultdict(list)
    for rule in rules:
        for record in records:
            key = rule.key(record)
            if key:
                index[key].append(record.key)
    return dict(index)
