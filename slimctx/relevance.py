"""Relevance and importance scoring — pure algorithms, no ML model.

Two signals, combined by the compressors:

1. BM25 over the batch being compressed (real IDF from the actual corpus,
   not a fixed constant): scores each item against the user's query/context.
2. Salience keywords: errors, warnings, security terms — matched on word
   boundaries so "authorization" doesn't fire on "author".

Exact-match identifiers (UUIDs, long hex, ticket IDs) get a boost because
for agent workloads the item containing *the* ID the user asked about is
almost always the answer.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Sequence

_TOKEN_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"  # UUID
    r"|[0-9a-fA-F]{12,}"  # long hex (hashes, ids)
    r"|\d+"  # numbers (IDs are often short — IDF handles common ones)
    r"|[A-Za-z][A-Za-z0-9_]*"  # identifiers/words
)

# priority weights, matched case-insensitively on token boundaries
SALIENCE = {
    "fatal": 1.0, "panic": 1.0, "critical": 0.95, "crash": 0.95,
    "error": 0.9, "exception": 0.9, "traceback": 0.9, "failed": 0.85,
    "failure": 0.85, "fail": 0.8, "abort": 0.8, "aborted": 0.8,
    "timeout": 0.75, "denied": 0.75, "rejected": 0.75, "refused": 0.75,
    "warn": 0.6, "warning": 0.6, "deprecated": 0.5,
    "todo": 0.4, "fixme": 0.45, "important": 0.4, "note": 0.3,
}


def tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _stem(word: str) -> str:
    """Tiny suffix-stripper so 'caching' matches 'cache', 'failed' matches
    'fail'. Deliberately conservative: only used inside BM25 (salience
    keywords match on surface forms)."""
    for suffix in ("ingly", "edly", "ing", "ed", "ies", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def salience_score(text: str) -> float:
    """Max salience keyword weight found in text (0.0 if none)."""
    best = 0.0
    for tok in tokenize(text):
        w = SALIENCE.get(tok)
        if w and w > best:
            best = w
            if best >= 1.0:
                break
    return best


class Bm25:
    """BM25 scoring of items against a query, IDF computed from the items."""

    K1 = 1.5
    B = 0.75

    def __init__(self, items: Sequence[str]):
        self._docs = [[_stem(t) for t in tokenize(t_)] for t_ in items]
        self._avg_len = (
            sum(len(d) for d in self._docs) / len(self._docs) if self._docs else 1.0
        ) or 1.0
        df: Dict[str, int] = {}
        for doc in self._docs:
            for tok in set(doc):
                df[tok] = df.get(tok, 0) + 1
        n = len(self._docs)
        self._idf = {
            tok: math.log(1 + (n - d + 0.5) / (d + 0.5)) for tok, d in df.items()
        }

    def score(self, index: int, query: str) -> float:
        """Normalized [0,1] BM25 score of item *index* against *query*."""
        doc = self._docs[index]
        if not doc:
            return 0.0
        counts: Dict[str, int] = {}
        for tok in doc:
            counts[tok] = counts.get(tok, 0) + 1
        raw = 0.0
        matched_long_id = False
        for q in {_stem(t) for t in tokenize(query)}:
            tf = counts.get(q, 0)
            if tf == 0:
                continue
            if len(q) >= 12 or "-" in q:
                matched_long_id = True
            idf = self._idf.get(q, 0.0)
            raw += idf * tf * (self.K1 + 1) / (
                tf + self.K1 * (1 - self.B + self.B * len(doc) / self._avg_len)
            )
        score = 1 - math.exp(-raw / 3.0)  # squash to [0,1)
        if matched_long_id:
            score = min(1.0, score + 0.35)  # exact-ID hit is nearly decisive
        return score
