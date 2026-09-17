"""String similarity primitives used by the comparison levels.

Small, dependency-free implementations.  ``jaro_winkler`` is the workhorse for
names; ``token_set_ratio`` handles company names where word order varies.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=100_000)
def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    window = max(len1, len2) // 2 - 1
    window = max(window, 0)
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0

    for i, ch in enumerate(s1):
        start = max(0, i - window)
        end = min(i + window + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s2[j] != ch:
                continue
            s1_matches[i] = s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    return (
        matches / len1 + matches / len2 + (matches - transpositions) / matches
    ) / 3.0


@lru_cache(maxsize=100_000)
def jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Jaro with a bonus for a shared prefix -- the usual choice for names."""
    score = jaro(s1, s2)
    if score < 0.7:
        return score
    prefix = 0
    for a, b in zip(s1[:4], s2[:4]):
        if a != b:
            break
        prefix += 1
    return score + prefix * prefix_weight * (1 - score)


@lru_cache(maxsize=100_000)
def levenshtein(s1: str, s2: str) -> int:
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)
    previous = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current = [i + 1]
        for j, c2 in enumerate(s2):
            current.append(
                min(previous[j + 1] + 1, current[j] + 1, previous[j] + (c1 != c2))
            )
        previous = current
    return previous[-1]


def levenshtein_ratio(s1: str, s2: str) -> float:
    if not s1 and not s2:
        return 1.0
    return 1.0 - levenshtein(s1, s2) / max(len(s1), len(s2))


def token_set_ratio(s1: str, s2: str) -> float:
    """Jaccard over whitespace tokens: order-insensitive, for company names."""
    t1, t2 = set(s1.split()), set(s2.split())
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def is_initial_of(short: str, long: str) -> bool:
    """``"j"`` is an initial of ``"john"``; ``"jo"`` is not."""
    return bool(short) and len(short) == 1 and long.startswith(short)
