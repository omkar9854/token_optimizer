"""Log compression via template mining (Drain-lite).

Agent tool output is dominated by *repeated* log lines that differ only in
volatile fields (timestamps, IDs, durations). Generic text compressors
score and drop individual sentences; they can't exploit this structure.
We can:

    2026-07-19T10:00:01Z GET /api/users/8231 200 12ms
    2026-07-19T10:00:02Z GET /api/users/9917 200 9ms
    ... x 1430 more ...

becomes one line:

    <TS> GET /api/users/<NUM> 200 <NUM>ms   [x1432]

Algorithm:
1. Mask volatile tokens per line (timestamps, UUIDs, hex, numbers, IPs).
2. Group lines by masked template.
3. Emit each template once with its count and 1-2 verbatim examples.
4. High-salience lines (errors, warnings, tracebacks) are NEVER collapsed —
   they pass through verbatim, in original order, because they are the
   signal the model is being asked to find.

Lossy — so the original is always placed in the store first.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List

from .relevance import salience_score

_VOLATILE = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<HEX>"),
    # No trailing \b: numbers fused to units ("12ms", "512Mi") must mask
    # too, or every line becomes its own template.
    (re.compile(r"\d+(?:\.\d+)?"), "<NUM>"),
]

# A template must repeat at least this many times before we collapse it;
# below that, collapsing costs more tokens (marker overhead) than it saves.
MIN_REPEAT = 3


def _template_of(line: str) -> str:
    for pattern, token in _VOLATILE:
        line = pattern.sub(token, line)
    return line


@dataclass
class _Group:
    template: str
    count: int = 0
    first_example: str = ""
    last_example: str = ""
    first_index: int = 0
    salient_lines: List[str] = field(default_factory=list)


def looks_like_log(text: str) -> bool:
    """Heuristic: many short lines, high proportion with timestamps/levels."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 10:
        return False
    hits = 0
    probe = lines[:50]
    level_re = re.compile(r"\b(INFO|DEBUG|WARN|WARNING|ERROR|FATAL|TRACE)\b")
    for ln in probe:
        if _VOLATILE[0][0].search(ln) or level_re.search(ln):
            hits += 1
    return hits >= len(probe) * 0.4


def compress_log(text: str, max_examples_per_template: int = 1) -> str:
    """Collapse repeated log templates; keep salient lines verbatim."""
    lines = text.splitlines()
    groups: "OrderedDict[str, _Group]" = OrderedDict()
    salient: List[tuple] = []  # (index, line) — kept verbatim, in order

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if salience_score(line) >= 0.6:
            salient.append((i, line))
            continue
        tpl = _template_of(line)
        grp = groups.get(tpl)
        if grp is None:
            grp = _Group(template=tpl, first_example=line, first_index=i)
            groups[tpl] = grp
        grp.count += 1
        grp.last_example = line

    out: List[str] = []
    emitted_salient = 0

    # Interleave: emit groups in first-appearance order, and salient lines
    # at their original positions relative to the groups around them.
    events: List[tuple] = []
    for grp in groups.values():
        events.append((grp.first_index, "group", grp))
    for idx, line in salient:
        events.append((idx, "salient", line))
    events.sort(key=lambda e: e[0])

    for _, kind, payload in events:
        if kind == "salient":
            out.append(payload)
            emitted_salient += 1
        else:
            grp = payload
            if grp.count < MIN_REPEAT:
                out.append(grp.first_example)
                if grp.count == 2:
                    out.append(grp.last_example)
            else:
                out.append(f"{grp.template}   [x{grp.count}]")
                if max_examples_per_template >= 1:
                    out.append(f"  e.g. {grp.first_example}")

    header = (
        f"[log summary: {len(lines)} lines -> {len(out)} shown; "
        f"{len(groups)} unique patterns; {emitted_salient} error/warn lines kept verbatim]"
    )
    return header + "\n" + "\n".join(out)
