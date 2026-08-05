import hashlib
import logging
from typing import List

import numpy as np

logger = logging.getLogger(__name__)


def h64(s: str) -> int:
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big", signed=True)


def ngrams(s: str, n: int = 3) -> set:
    s = f" {s} "
    if len(s) <= n:
        return {s}
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def trigram_sim(a: str, b: str) -> float:
    A = ngrams(a)
    B = ngrams(b)
    if not A and not B:
        return 1.0
    return len(A & B) / len(A | B)


def row_similarity(tmpl: str, db: str, n: int = 3) -> float:
    """One-directional containment of template ngrams inside DB cell ngrams.

    Ideal for row-level verification and highlighting where a short template
    value (e.g. 'Аленкинское') matches a longer DB cell (e.g.
    'ТМСК Внеш_тран_Аленкинское') without the massive Jaccard length penalty.
    """
    A = ngrams(tmpl, n)
    B = ngrams(db, n)
    if not A:
        return 1.0 if not B else 0.0
    return len(A & B) / len(A)


def add_ngram_hashes(values, target: set, n: int = 3) -> None:
    """Add h64 of every n-gram of each value to `target` (stream-friendly)."""
    for v in values:
        for ng in ngrams(v, n):
            target.add(h64(ng))


def build_ngram_hashes(values: List[str], n: int = 3) -> np.ndarray:
    hashes: set = set()
    add_ngram_hashes(values, hashes, n)
    return np.sort(np.array(list(hashes), dtype=np.int64))


def shared_trigram_spans(a: str, b: str, n: int = 3) -> List[tuple]:
    """Return merged (start, end) char ranges in `a` covered by trigrams shared with `b`.

    Trigrams are computed on the space-padded strings (like pg_trgm), so a
    trigram at padded index i maps to original chars [i-1, i-1+n) clipped to
    [0, len(a)). Overlapping ranges are merged.
    """
    if not a:
        return []
    shared = ngrams(a, n) & ngrams(b, n)
    if not shared:
        return []

    padded = f" {a} "
    ranges: List[tuple] = []
    for i in range(len(padded) - n + 1):
        if padded[i : i + n] in shared:
            start = max(0, i - 1)
            end = min(len(a), i - 1 + n)
            if end > start:
                ranges.append((start, end))

    if not ranges:
        return []

    ranges.sort()
    merged = [ranges[0]]
    for s, e in ranges[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged
