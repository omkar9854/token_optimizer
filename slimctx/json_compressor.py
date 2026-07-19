"""JSON compression: lossless tabularization first, relevance-ranked
row selection second.

Stage 1 (lossless): a homogeneous array of objects repeats every key name
in every element. Re-emitting it as a header + rows table removes that
redundancy without dropping a single value:

    [{"id": 1, "name": "a", "ok": true}, ...]   ->
    #table cols=id|name|ok rows=200
    1|a|true
    ...

Nested objects are flattened to dotted columns (meta.region) when uniform.

Stage 2 (lossy, only if a row budget is given and exceeded): rank rows and
keep the best under budget. A row is kept when it (a) contains an error /
salient value, (b) is a statistical outlier on any numeric column (>2
sigma), (c) is in the head or tail (positional anchors), or (d) scores
high BM25 relevance against the query. Kept rows keep original order.

Everything else (heterogeneous arrays, scalars, deep trees) is minified
(no whitespace) — also lossless.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .relevance import Bm25, salience_score

CORE_FIELD_THRESHOLD = 0.8  # key must appear in >=80% of rows to be a column
HEAD_KEEP = 3
TAIL_KEEP = 2


def try_parse(text: str) -> Optional[Any]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # RecursionError: pathologically nested input (e.g. 100k open
        # brackets) must degrade to "not JSON", not crash the pipeline.
        return None


def _flatten(obj: Dict[str, Any], prefix: str = "", max_depth: int = 2) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and max_depth > 0 and 0 < len(v) <= 6:
            flat.update(_flatten(v, f"{key}.", max_depth - 1))
        else:
            flat[key] = v
    return flat


def _find_object_array(data: Any) -> Optional[Tuple[List[Dict[str, Any]], Optional[str]]]:
    """Return (array, wrapper_key) if data is/contains an array of objects."""
    if isinstance(data, list):
        if len(data) >= 2 and all(isinstance(x, dict) for x in data):
            return data, None
        return None
    if isinstance(data, dict):
        # common API shapes: {"items": [...]}, {"results": [...]}, etc.
        candidates = [
            (k, v) for k, v in data.items()
            if isinstance(v, list) and len(v) >= 2 and all(isinstance(x, dict) for x in v)
        ]
        if len(candidates) == 1:
            k, v = candidates[0]
            return v, k
    return None


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return value.replace("|", "\\|").replace("\n", "\\n")
    return json.dumps(value, separators=(",", ":"))


def _numeric_outlier_rows(rows: List[Dict[str, Any]], cols: Sequence[str]) -> set:
    """Indices of rows that are >2 sigma from the mean on any numeric column."""
    outliers: set = set()
    for col in cols:
        values = []
        for i, row in enumerate(rows):
            v = row.get(col)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append((i, float(v)))
        if len(values) < 8:
            continue
        nums = [v for _, v in values]
        mean = sum(nums) / len(nums)
        var = sum((x - mean) ** 2 for x in nums) / len(nums)
        std = math.sqrt(var)
        if std == 0:
            continue
        for i, v in values:
            if abs(v - mean) > 2 * std:
                outliers.add(i)
    return outliers


def tabularize(
    text: str,
    query: str = "",
    max_rows: Optional[int] = None,
) -> Optional[Tuple[str, bool]]:
    """Compress a JSON string. Returns (compressed, was_lossy) or None if
    the input is not JSON we can improve."""
    data = try_parse(text)
    if data is None:
        return None

    found = _find_object_array(data)
    if found is None:
        minified = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if len(minified) < len(text) * 0.9:
            return minified, False
        return None

    rows_raw, wrapper = found
    rows = [_flatten(r) for r in rows_raw]

    # Determine core columns (present in >= threshold of rows).
    freq: Dict[str, int] = {}
    for row in rows:
        for k in row:
            freq[k] = freq.get(k, 0) + 1
    n = len(rows)
    cols = [k for k, c in freq.items() if c >= n * CORE_FIELD_THRESHOLD]
    if len(cols) < 2:
        return None  # too heterogeneous to tabularize profitably
    # Stable column order: order of first appearance.
    seen_order: List[str] = []
    for row in rows:
        for k in row:
            if k in cols and k not in seen_order:
                seen_order.append(k)
    cols = seen_order

    # Constant-column extraction (lossless): a column whose value is
    # identical in every row is stated once in a legend, not repeated
    # per row. Common in API payloads (repo, branch, region, type...).
    consts: Dict[str, Any] = {}
    for col in list(cols):
        if freq.get(col) == n:
            first = rows[0].get(col)
            if all(row.get(col) == first for row in rows):
                consts[col] = first
                cols.remove(col)
    if len(cols) < 1:
        return None

    kept_indices = list(range(n))
    was_lossy = False
    if max_rows is not None and n > max_rows:
        kept_indices = _select_rows(rows, cols, query, max_rows)
        was_lossy = True

    lines = []
    label = f" key={wrapper}" if wrapper else ""
    shown = len(kept_indices)
    row_note = f"rows={n}" if not was_lossy else f"rows={shown}of{n}"
    lines.append(f"#table{label} cols={'|'.join(cols)} {row_note}")
    if consts:
        legend = " | ".join(f"{k}={_cell(v)}" for k, v in consts.items())
        lines.append(f"#every-row: {legend}")
    prev = -1
    for i in kept_indices:
        if was_lossy and prev >= 0 and i != prev + 1:
            lines.append(f"…({i - prev - 1} rows omitted)…")
        row = rows[i]
        cells = [_cell(row.get(c)) for c in cols]
        extras = {k: v for k, v in row.items() if k not in cols and k not in consts}
        line = "|".join(cells)
        if extras:
            line += " +" + json.dumps(extras, separators=(",", ":"), ensure_ascii=False)
        lines.append(line)
        prev = i
    return "\n".join(lines), was_lossy


def _select_rows(
    rows: List[Dict[str, Any]], cols: Sequence[str], query: str, max_rows: int
) -> List[int]:
    n = len(rows)
    keep: set = set(range(min(HEAD_KEEP, n))) | set(range(max(0, n - TAIL_KEEP), n))

    row_texts = [json.dumps(r, ensure_ascii=False) for r in rows]
    # Salience pins a row only when it is *distinctive*: a keyword that
    # appears in most rows (e.g. `timeout=30` in every signature) carries
    # no information about which rows matter.
    salient = [i for i, txt in enumerate(row_texts) if salience_score(txt) >= 0.75]
    if len(salient) <= max(3, n // 5):
        keep.update(salient)
    keep |= _numeric_outlier_rows(rows, cols)

    if query:
        bm25 = Bm25(row_texts)
        scored = sorted(
            ((bm25.score(i, query), i) for i in range(n) if i not in keep),
            reverse=True,
        )
        for score, i in scored:
            if score <= 0.15:
                break
            # Strong matches always earn a slot, even past the row budget —
            # the row that answers the query is the last thing to sacrifice.
            if score >= 0.5 or len(keep) < max_rows:
                keep.add(i)
            elif len(keep) >= max_rows:
                break

    # Fill remaining budget with evenly spaced samples for coverage.
    if len(keep) < max_rows:
        remaining = [i for i in range(n) if i not in keep]
        want = max_rows - len(keep)
        if remaining and want > 0:
            step = max(1, len(remaining) // want)
            for i in remaining[::step][:want]:
                keep.add(i)

    return sorted(keep)
