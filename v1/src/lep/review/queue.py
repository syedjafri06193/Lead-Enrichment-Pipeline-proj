"""The review queue (design.md sections 5.4, 17.2).

Build this **before** auto-merge.  Route everything through review first,
measure precision in each score band on real decisions, and only then promote a
band to automatic.  The precision number that gates auto-merge has to come from
adjudicated decisions on your data -- not from a benchmark, not from a paper,
and not from the model's own confidence.

Reviewer decisions are doing double duty: they are the precision measurement
*and* they are labelled training data for the next model.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal, Sequence

from lep.core.types import utcnow
from lep.identity.policy import MatchPolicy, promote_band, score_band
from lep.store.db import Database, dumps, from_iso, loads, to_iso

ItemKind = Literal["pair", "cluster", "delete"]
Status = Literal["pending", "decided", "deferred"]


@dataclass
class ReviewItem:
    kind: ItemKind
    left_key: str
    right_key: str | None
    score: float | None
    payload: dict[str, Any]
    reason: str
    status: Status = "pending"
    priority: float = 0.0
    created_at: datetime = field(default_factory=utcnow)
    id: int | None = None


@dataclass
class ReviewDecision:
    item_id: int
    model_score: float | None
    human_said_match: bool
    reviewer: str
    note: str | None = None
    decided_at: datetime = field(default_factory=utcnow)
    id: int | None = None


class ReviewQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ enqueue

    def enqueue(
        self,
        kind: ItemKind,
        left_key: str,
        *,
        right_key: str | None = None,
        score: float | None = None,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        priority: float | None = None,
    ) -> ReviewItem:
        """Add an item.

        ``priority`` defaults to model uncertainty: pairs closest to the
        decision boundary first.  That is the cheap version of active learning
        from the stretch-goal table -- dramatically more labels per hour of
        reviewer time than working through a queue in arrival order.
        """
        if priority is None:
            priority = 1.0 - abs((score if score is not None else 0.5) - 0.5) * 2
        item = ReviewItem(
            kind=kind,
            left_key=left_key,
            right_key=right_key,
            score=score,
            payload=payload or {},
            reason=reason,
            priority=priority,
        )
        cur = self.db.execute(
            """
            INSERT INTO review_items
                (kind, left_key, right_key, score, payload, reason, status,
                 created_at, priority)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                item.kind,
                item.left_key,
                item.right_key,
                item.score,
                dumps(item.payload),
                item.reason,
                to_iso(item.created_at),
                item.priority,
            ),
        )
        item.id = int(cur.lastrowid)
        return item

    def enqueue_pair(self, pair, reason: str = "score in review band") -> ReviewItem:
        """Convenience for a :class:`~lep.identity.scorer.ScoredPair`."""
        return self.enqueue(
            "pair",
            pair.left,
            right_key=pair.right,
            score=pair.probability,
            reason=reason,
            payload={"levels": pair.levels, "explanation": pair.explain()},
        )

    def enqueue_cluster(self, cluster: Iterable[str], verdict: str) -> ReviewItem:
        members = sorted(cluster)
        return self.enqueue(
            "cluster",
            members[0],
            reason=f"cluster guard: {verdict}",
            payload={"members": members, "verdict": verdict},
            priority=0.9,
        )

    def enqueue_delete(self, source: str, record_id: str, reason: str) -> ReviewItem:
        """Deletes are conflicts too (section 8.4), and the costly kind.

        Never propagate a hard delete automatically.  An unwanted propagated
        delete destroys data across several systems at once; an un-propagated
        one is untidy.
        """
        return self.enqueue(
            "delete",
            f"{source}:{record_id}",
            reason=reason,
            payload={"source": source, "record_id": record_id},
            priority=1.0,
        )

    # --------------------------------------------------------------- read

    def pending(self, limit: int = 50, kind: ItemKind | None = None) -> list[ReviewItem]:
        sql = "SELECT * FROM review_items WHERE status = 'pending'"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY priority DESC, id LIMIT ?"
        params.append(limit)
        return [self._item(r) for r in self.db.query(sql, params)]

    def get(self, item_id: int) -> ReviewItem | None:
        row = self.db.one("SELECT * FROM review_items WHERE id = ?", (item_id,))
        return self._item(row) if row else None

    def depth(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM review_items WHERE status = 'pending'"
            )
            or 0
        )

    # ------------------------------------------------------------- decide

    def decide(
        self,
        item_id: int,
        human_said_match: bool,
        reviewer: str,
        note: str | None = None,
    ) -> ReviewDecision:
        item = self.get(item_id)
        if item is None:  # pragma: no cover - programming error
            raise KeyError(f"no review item {item_id}")
        decision = ReviewDecision(
            item_id=item_id,
            model_score=item.score,
            human_said_match=human_said_match,
            reviewer=reviewer,
            note=note,
        )
        cur = self.db.execute(
            """
            INSERT INTO review_decisions
                (item_id, model_score, human_match, reviewer, note, decided_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                decision.model_score,
                int(human_said_match),
                reviewer,
                note,
                to_iso(decision.decided_at),
            ),
        )
        decision.id = int(cur.lastrowid)
        self.db.execute(
            "UPDATE review_items SET status = 'decided' WHERE id = ?", (item_id,)
        )
        return decision

    def defer(self, item_id: int) -> None:
        self.db.execute(
            "UPDATE review_items SET status = 'deferred' WHERE id = ?", (item_id,)
        )

    def decisions(self) -> list[ReviewDecision]:
        rows = self.db.query("SELECT * FROM review_decisions ORDER BY id")
        return [
            ReviewDecision(
                id=r["id"],
                item_id=r["item_id"],
                model_score=r["model_score"],
                human_said_match=bool(r["human_match"]),
                reviewer=r["reviewer"],
                note=r["note"],
                decided_at=from_iso(r["decided_at"]),
            )
            for r in rows
        ]

    def labels(self) -> list[tuple[str, str, bool]]:
        """Adjudicated decisions as training labels for the next model."""
        rows = self.db.query(
            """
            SELECT i.left_key, i.right_key, d.human_match
            FROM review_decisions d JOIN review_items i ON i.id = d.item_id
            WHERE i.kind = 'pair' AND i.right_key IS NOT NULL
            """
        )
        return [(r["left_key"], r["right_key"], bool(r["human_match"])) for r in rows]

    # -------------------------------------------------------- measurement

    def precision_by_band(self) -> dict[str, float]:
        return precision_by_band(self.decisions())

    def counts_by_band(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for decision in self.decisions():
            if decision.model_score is None:
                continue
            counts[score_band(decision.model_score)] += 1
        return dict(sorted(counts.items()))

    def promotion_report(self, policy: MatchPolicy = MatchPolicy()) -> list[str]:
        """What the measured data says about turning auto-merge on.

        This is the only acceptable input to that decision.
        """
        precision = self.precision_by_band()
        counts = self.counts_by_band()
        lines = []
        for band in sorted(precision, reverse=True):
            ok, message = promote_band(band, precision[band], counts.get(band, 0), policy)
            lines.append(("PROMOTE  " if ok else "HOLD     ") + message)
        if not lines:
            lines.append("HOLD     no adjudicated decisions yet: auto-merge stays off")
        return lines

    def _item(self, row: Any) -> ReviewItem:
        return ReviewItem(
            id=row["id"],
            kind=row["kind"],
            left_key=row["left_key"],
            right_key=row["right_key"],
            score=row["score"],
            payload=loads(row["payload"]) or {},
            reason=row["reason"],
            status=row["status"],
            created_at=from_iso(row["created_at"]),
            priority=row["priority"],
        )


def precision_by_band(decisions: Sequence[ReviewDecision]) -> dict[str, float]:
    """Reviewer decisions grouped by the score the model assigned.

    THIS is what justifies raising or lowering ``auto_merge_threshold``
    (section 17.2).  Review it after every model change: a threshold set once
    and never revisited drifts out of calibration as the data changes.
    """
    bands: dict[str, dict[str, int]] = defaultdict(lambda: {"merge": 0, "split": 0})
    for decision in decisions:
        if decision.model_score is None:
            continue
        band = score_band(decision.model_score)
        bands[band]["merge" if decision.human_said_match else "split"] += 1
    return {
        band: counts["merge"] / (counts["merge"] + counts["split"])
        for band, counts in sorted(bands.items())
        if counts["merge"] + counts["split"]
    }
