"""Salesforce adapter (design.md sections 3.1, 7.3, 12.1).

The production client is ``simple-salesforce``; what matters architecturally is
here:

* ``Sforce-Limit-Info`` on every response is the authoritative usage figure,
  and it is the *org's* usage, pooled across REST, SOAP, Bulk and Connect --
  every integration on the account, not just this one.
* change detection prefers Change Data Capture and falls back to polling
  ``SystemModstamp`` (not ``LastModifiedDate``, which misses system-level
  changes).
* field metadata is read at startup and periodically, never hardcoded, and a
  mapping that references a missing field fails loudly rather than silently
  skipping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from lep.sync.crm import FakeCrm


class SchemaDriftError(Exception):
    """A mapped field is not present in the live metadata (section 12.1)."""


@dataclass
class FieldMetadata:
    name: str
    type: str = "string"
    picklist_values: tuple[str, ...] = ()
    active: bool = True


@dataclass
class MappingVersion:
    """Versioned field mapping.

    Section 12.1: version the mapping and record which version produced each
    observation.  Without it, a mapping change makes every historical
    observation ambiguous.
    """

    version: str
    fields: dict[str, str] = field(default_factory=dict)  # our field -> CRM field

    def validate(self, metadata: dict[str, FieldMetadata]) -> None:
        missing = [
            crm_field
            for crm_field in self.fields.values()
            if crm_field not in metadata or not metadata[crm_field].active
        ]
        if missing:
            # Fail loudly.  Silently skipping a missing field is how a rename
            # turns into three weeks of quietly unsynced data.
            raise SchemaDriftError(
                f"mapping {self.version} references fields absent or inactive in "
                f"Salesforce metadata: {', '.join(sorted(missing))}"
            )


class FakeSalesforce(FakeCrm):
    """Salesforce-shaped fake: limit headers, SystemModstamp, picklists."""

    def __init__(self, *, daily_limit: int = 15_000, **kwargs: Any) -> None:
        kwargs.setdefault("integration_user_id", "0051t000001SfIntegration")
        super().__init__("salesforce", daily_limit=daily_limit, **kwargs)
        self.metadata: dict[str, FieldMetadata] = {
            "Email": FieldMetadata("Email"),
            "FirstName": FieldMetadata("FirstName"),
            "LastName": FieldMetadata("LastName"),
            "Phone": FieldMetadata("Phone"),
            "Title": FieldMetadata("Title"),
            "OwnerId": FieldMetadata("OwnerId"),
            "LeadSource": FieldMetadata(
                "LeadSource", "picklist", ("Web", "Referral", "Partner")
            ),
        }

    def headers(self) -> dict[str, str]:
        return {"Sforce-Limit-Info": f"api-usage={self.calls}/{self.daily_limit}"}

    def describe(self) -> dict[str, FieldMetadata]:
        """Read field metadata at startup and periodically -- never hardcode it."""
        self.calls += 1
        return dict(self.metadata)

    def rename_field(self, old: str, new: str) -> None:
        """A rename looks like a delete plus an add, and orphans your mapping."""
        meta = self.metadata.pop(old, None)
        if meta:
            self.metadata[new] = FieldMetadata(new, meta.type, meta.picklist_values)

    def add_picklist_value(self, field_name: str, value: str) -> None:
        meta = self.metadata.get(field_name)
        if meta and meta.type == "picklist":
            self.metadata[field_name] = FieldMetadata(
                meta.name, meta.type, meta.picklist_values + (value,)
            )

    def write(self, record_id: str, field_name: str, value: Any, **kwargs: Any):
        meta = self.metadata.get(field_name)
        if meta and meta.type == "picklist" and value not in meta.picklist_values:
            # Writing an invalid picklist value errors; reading an unknown one
            # must not crash (section 12.1).
            raise SchemaDriftError(
                f"{value!r} is not a valid picklist value for {field_name}: "
                f"{meta.picklist_values}"
            )
        return super().write(record_id, field_name, value, **kwargs)


def bulk_job_count(records: int, per_job: int = 10_000) -> int:
    """Bulk API 2.0 jobs needed for ``records`` (section 3.4)."""
    return max(1, -(-records // per_job))


def batched(items: Iterable[str], size: int = 200) -> list[list[str]]:
    """``WHERE Id IN (...)`` batching: 200 records is one call, not 200."""
    batch: list[str] = []
    out: list[list[str]] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            out.append(batch)
            batch = []
    if batch:
        out.append(batch)
    return out
