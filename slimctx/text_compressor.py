"""Extractive text compression.

Splits prose into sentences, scores each on three signals, keeps the best
under a target ratio, and re-joins survivors in original order (verbatim —
we never paraphrase, so nothing is hallucinated):

  * relevance  — BM25 against the caller's query, IDF from this document
  * salience   — error/warning/instructional keyword weight
  * position   — lead bias (openings state the topic) + recency bias
                 (endings state conclusions)

The caller stores the original before calling this, so dropped sentences
remain retrievable.
"""

from __future__ import annotations

import re
from typing import List

from .relevance import Bm25, salience_score

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n{2,}|\n(?=[-*#>\d])")


def split_sentences(text: str) -> List[str]:
    parts = _SENTENCE_END.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def compress_text(text: str, query: str = "", target_ratio: float = 0.4) -> str:
    """Keep roughly *target_ratio* of the text, by sentence."""
    sentences = split_sentences(text)
    n = len(sentences)
    if n <= 3:
        return text

    bm25 = Bm25(sentences)
    scores = []
    for i, sent in enumerate(sentences):
        relevance = bm25.score(i, query) if query else 0.0
        salience = salience_score(sent)
        lead = max(0.0, 1.0 - i / 3.0) * 0.2        # first 3 sentences
        recency = max(0.0, 1.0 - (n - 1 - i) / 3.0) * 0.2  # last 3
        # Query relevance dominates when a query exists; position is a
        # tie-breaker, not a driver — otherwise openings/endings crowd out
        # the sentences that actually answer the question.
        score = 0.6 * relevance + 0.35 * salience + lead + recency
        scores.append((score, i))

    budget = max(3, int(n * target_ratio))
    keep = {i for _, i in sorted(scores, reverse=True)[:budget]}

    out: List[str] = []
    gap = 0
    for i, sent in enumerate(sentences):
        if i in keep:
            if gap:
                out.append(f"[…{gap} sentence{'s' if gap > 1 else ''} omitted…]")
                gap = 0
            out.append(sent)
        else:
            gap += 1
    if gap:
        out.append(f"[…{gap} sentence{'s' if gap > 1 else ''} omitted…]")
    return "\n".join(out)
