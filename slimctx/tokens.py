"""Token estimation without any external tokenizer.

Deliberately dependency-free: in a client environment we cannot assume
network access to download tokenizer vocabularies, and an estimate that is
consistently within ~10% of tiktoken/claude tokenizers is good enough for
budgeting decisions (we compress *relative* to a budget, we never bill by it).

Calibration: English prose averages ~4 chars/token; code and JSON average
~3.2 chars/token because of punctuation-heavy vocabularies. We blend a
character-based and a word/symbol-based estimate and take the max, which
tracks real tokenizers well on both prose and structured content.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


def estimate_tokens(text: str) -> int:
    """Estimate the LLM token count of *text*.

    Returns 0 for empty input. Never returns less than 1 for non-empty input.
    """
    if not text:
        return 0
    pieces = _WORD_RE.findall(text)
    # Long identifiers split into multiple subword tokens (~6 chars each).
    subword = sum(1 + (len(p) - 1) // 6 for p in pieces)
    by_chars = len(text) // 4
    return max(1, subword * 3 // 4, by_chars)
