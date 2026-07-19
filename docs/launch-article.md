---
title: How I cut my AI agent's log context 99.5% with stdlib Python
published: true
tags: ai, python, llm, opensource
canonical_url: https://github.com/omkar9854/token_optimizer/blob/main/docs/launch-article.md
---

*This article lives in the [token_optimizer repo](https://github.com/omkar9854/token_optimizer); everything below is reproducible from it.*

## The problem: agents re-read everything

Watch a coding agent debug a production incident and you'll see the same
pattern: it pulls a 60,000-token log dump into context, finds the one FATAL
line, and then **drags all 60,000 tokens along for every subsequent turn**.
You pay for that log five, ten, twenty times. Multiply by JSON tool
outputs, file reads, and search results, and most of an agent's bill is
re-reading things it already understood.

Context-compression middleware exists, but the main player pulls a 261MB
ONNX model from HuggingFace at first run. I deploy into client
environments — air-gapped, security-reviewed, every dependency audited.
"It downloads a model at runtime" is a non-starter. So I built the thing I
could actually ship: **slimctx**, ~1,600 lines of pure stdlib Python.
Zero dependencies. Zero network calls. The whole audit surface fits in an
afternoon.

```
pip install slimctx
```

## The trick: content-aware compression, not truncation

Truncating context is easy and wrong — the FATAL line is usually in the
part you cut. slimctx routes each blob by detected type:

**Logs → template mining.** Agent tool output is dominated by repeated
lines differing only in volatile fields. Mask timestamps, IDs, and numbers,
group by template, and 2,500 lines become:

```
<TS> INFO [api-gw] request_id=<HEX> GET /v2/orders in <NUM>ms   [x625]
<TS> INFO [checkout] request_id=<HEX> GET /v2/orders in <NUM>ms [x625]
2026-07-19T14:30:00Z FATAL [checkout] OOMKilled: exceeded memory 512Mi
```

Error and warning lines are **never** collapsed — they pass through
verbatim. That's the demo in the repo: **61,700 tokens → 298, in 29ms**,
FATAL intact.

**JSON → lossless tabularization first.** An array of objects repeats
every key in every element. Re-emit it as header + rows and you save
40–60% *without dropping a single value*. Columns that are constant across
all rows get stated once in a legend. Only if a row budget is exceeded
does it fall back to lossy selection — and then errors, statistical
outliers (>2σ), head/tail anchors, and query-relevant rows are pinned.

**Code → query-aware skeletons.** Keep imports, signatures, and
docstrings; elide bodies — *except* the functions most relevant to what
the user asked, which stay verbatim. The model keeps the map plus the
exact code under discussion.

**Prose → extractive selection.** BM25 relevance + salience keywords +
position, keeping sentences verbatim. No paraphrasing, so nothing can be
hallucinated into the context.

## The invariant that makes it safe: universal reversibility

Every lossy transform first stores the original in a content-addressed
store (SHA-256, in-memory or SQLite) and stamps the output with a marker:

```
[slimctx-ref 3e4e0f8067f5535f7daf82ae original~61700tok type=log;
 call retrieve('3e4e0f8067f5535f7daf82ae') for full content]
```

The model — or you — can always get the byte-exact original back. This is
enforced by a test that compresses a conversation and round-trips every
ref. Compression you can undo is compression you can trust in production.

Three more invariants, each with a test that fails if violated:

1. **Errors are never dropped** — by any compressor, on any path.
2. **Deterministic output** — same input, byte-identical output, across
   processes. Sounds pedantic; it's actually money: provider prompt caches
   key on exact prefixes, and a compressor that renders the same message
   differently each turn silently destroys your cache hit rate.
3. **Net gain or no-op** — if a transform doesn't save enough tokens to
   pay for its own marker, the original is kept. The last N messages are
   never touched at all.

## Two bugs worth telling on myself

**The salience backfire.** Rows containing error keywords get pinned
during lossy JSON selection. Then a code-search benchmark put `timeout=30`
in every result row — "timeout" is a salience keyword, so 99 of 100 rows
got pinned as "important" and crowded out the actual answer. Fix: salience
only pins rows when it's *distinctive* — a keyword present in most rows
carries no information. Heuristics need denominators.

**The \b that ate 3x.** Log templates masked numbers with `\b\d+\b`. But
`12ms` has no word boundary between `2` and `m` — so durations never
masked, every line became its own unique template, and template mining was
silently doing nothing. Savings looked "fine" (82%) because a fallback
sampler was carrying it. Removing one `\b` took the SRE workload to 99.5%.
If I hadn't checked *why* the number was 82 and not 95, I'd never have
found it. Benchmarks that only assert "pretty good" hide broken cores.

## Numbers

| Workload | Before | After | Savings |
|---|---:|---:|---:|
| Code search (100 results) | 5,557 | 916 | 84% |
| SRE incident debugging | 61,699 | 298 | 99.5% |
| GitHub issue triage | 12,836 | 975 | 92% |
| Codebase exploration | 5,734 | 2,760 | 52% |

Synthetic workloads, clearly labeled as such — but every run plants
"needles" (a FIXME, an OOMKill, a latency outlier) and asserts they
survive compression, and every lossy transform is verified byte-exact
reversible. Reproduce with `python3 benchmarks/bench.py`. I would genuinely
like to see numbers from your real workloads — that's the honest next
test.

## Using it

Library:

```python
from slimctx import Pipeline, Config

pipe = Pipeline(Config(target_tokens=32_000))
result = pipe.compress(messages)      # OpenAI/Anthropic-style dicts
pipe.retrieve(ref)                    # byte-exact original, any time
```

Or as an MCP server for GitHub Copilot agent mode, Claude Code, or Cursor
(`uvx slimctx-mcp`) — it's on the official MCP Registry as
`io.github.omkar9854/token_optimizer`. Three tools: `slimctx_compress`,
`slimctx_retrieve`, `slimctx_stats`.

Repo, tests, benchmarks, security checklist:
**<https://github.com/omkar9854/token_optimizer>**

Apache-2.0. Issues and real-workload benchmark reports very welcome.
