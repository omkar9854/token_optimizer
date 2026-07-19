# Using slimctx

Three ways to use it, from simplest to most integrated.

---

## Prerequisites

**Required — this is the complete list:**

| Requirement | Minimum | Check with |
|---|---|---|
| Python | 3.9+ | `python3 --version` |
| OS | Linux / macOS / Windows | — |

That's it. No pip dependencies, no compilers, no ML models, no network
access at runtime, no API keys. The standard library is the entire
dependency tree.

**Optional, depending on how you deploy:**

- `pytest` — only to run the test suite before sign-off: `pip install pytest`
- `cryptography` (or any AEAD library you already trust) — only if the
  client requires the reversible store encrypted at rest
- VS Code + GitHub Copilot with agent mode — only for the GHCP/MCP route
  (Copilot Chat ≥ 0.26 / VS Code ≥ 1.99 for MCP support)

**Air-gapped / restricted client environments:**

1. Copy the repository in by any approved channel (it is a few hundred KB
   of readable Python — no binary artifacts to whitelist).
2. Either `pip install -e .` from the local copy, **or skip pip entirely**
   and vendor the `slimctx/` package folder next to your code — it is
   import-ready as-is.
3. Verify on-site, offline:
   ```bash
   python3 -m pytest tests/ -q        # 26 tests, all local
   python3 benchmarks/bench.py        # savings table, all local
   grep -rE "urllib|socket|requests" slimctx/ --include="*.py"   # comment-only hits
   ```
4. Decide where originals live: default is **in-memory** (nothing touches
   disk, gone on exit). Pass `--db`/`SqliteStore` only if cross-restart
   retrieval is required, and then apply the security checklist at the
   bottom of this file.

---

## 1. As a library (any Python app / agent framework)

Install (or just vendor the `slimctx/` folder — it is self-contained):

```bash
pip install -e /path/to/slimctx
```

Compress a message list right before your provider call:

```python
from slimctx import Pipeline, Config

pipe = Pipeline(Config(
    target_tokens=32_000,   # compress only when the conversation exceeds this
    live_messages=4,        # never touch the last 4 messages
))

result = pipe.compress(messages)         # [{"role": ..., "content": ...}, ...]
response = client.chat.completions.create(model=..., messages=result.messages)

print(f"saved {result.savings_ratio:.0%}")
for t in result.transforms:              # what was changed, and how
    print(t)                             # {"content_type": "log", "ref": "...", ...}
```

Compress a single blob (e.g. one big tool output):

```python
compact = pipe.compress_blob(huge_tool_output, query="what the user asked")
```

Recover any original — compressed output carries `[slimctx-ref <ref> ...]`
markers:

```python
original = pipe.retrieve("03e61ff491c00cf7246c51b7")   # byte-exact or None
```

Give the model retrieval ability by registering `pipe.retrieve` as a tool
named `retrieve`; the marker text tells the model exactly what to call.

## 2. In GitHub Copilot (GHCP)

slimctx ships an MCP server that Copilot's agent mode can call as tools.

**VS Code (Copilot agent mode).** Create `.vscode/mcp.json` in your
workspace:

```json
{
  "servers": {
    "slimctx": {
      "type": "stdio",
      "command": "python3",
      "args": [
        "-m", "slimctx.mcp_server",
        "--db", "${workspaceFolder}/.slimctx/store.db"
      ]
    }
  }
}
```

Make sure `slimctx` is importable by that `python3` (either `pip install -e`
it, or add `"env": {"PYTHONPATH": "/path/to/slimctx"}` to the server entry).
Create the folder once: `mkdir -p .slimctx` — and add `.slimctx/` to
`.gitignore` (the store holds originals of everything compressed; never
commit it).

Restart the MCP server from the Command Palette (*MCP: List Servers*) and
Copilot Chat's agent mode will offer three tools:

| Tool | What it does |
|---|---|
| `slimctx_compress` | Compress a blob (pass `query` for what you're looking for) |
| `slimctx_retrieve` | Get the byte-exact original for a `[slimctx-ref ...]` marker |
| `slimctx_stats` | Session token savings |

**Copilot coding agent (github.com).** In the repository settings →
Copilot → coding agent → MCP configuration, register the same command.
The agent's ephemeral environment has Python 3; point `command` at
`python3` and vendor slimctx in the repo so no install step is needed.

**Practical pattern:** ask Copilot to *"run the failing test, compress the
output with slimctx_compress (query: the assertion that failed), then
diagnose"* — large pytest/log output stops flooding the context window.

## 3. As an MCP server for any other client

Same server works for Claude Code, Cursor, or anything MCP-compatible:

```bash
python3 -m slimctx.mcp_server --db ~/.slimctx/store.db --target-tokens 32000
```

Claude Code registration:

```bash
claude mcp add slimctx -- python3 -m slimctx.mcp_server --db ~/.slimctx/store.db
```

## Configuration reference

| `Config` field | Default | Meaning |
|---|---:|---|
| `target_tokens` | 32,000 | Only compress when the conversation exceeds this |
| `live_messages` | 4 | Trailing messages that are never modified |
| `min_compress_tokens` | 256 | Blobs smaller than this pass through |
| `min_saving_tokens` | 48 | A transform must save at least this or it's discarded |
| `text_ratio` | 0.4 | Fraction of sentences kept in prose |
| `json_max_rows` | 30 | Row budget before JSON row-selection kicks in |
| `max_blob_bytes` | 8,000,000 | Blobs larger than this are passed through (DoS guard) |
| `memo_capacity` | 2,048 | Bound on the determinism memo |

## Security deployment checklist (client environments)

- **Store location.** Default is in-memory (originals vanish on exit). With
  `--db`/`SqliteStore`, the file is created `0600`, but it contains
  *originals of everything compressed* — put it on an encrypted volume,
  never in the repo, and let the 6-hour TTL do its job.
- **Encrypt at rest** if required:
  ```python
  from cryptography.fernet import Fernet   # your dependency, your keys
  f = Fernet(key)
  store = SqliteStore("store.db", cipher=(f.encrypt, f.decrypt))
  ```
- **Shared store?** Set `salt="<random>"` so refs are not guessable from
  content by other users of the same file.
- **No egress to verify.** The package makes zero network calls — confirm
  with `grep -rE "urllib|socket|http|requests" slimctx/` (no hits in code).
- **Prompt-injection note.** Compressed content is data; a malicious
  payload could *contain* fake `[slimctx-ref ...]` markers, but retrieval
  only ever returns content that actually passed through the store, and
  malformed refs are rejected. Treat retrieved content with the same
  trust level as the tool output it came from.

## Verify before deploying

```bash
python -m pytest tests/ -q      # 26 tests: invariants + security regressions
python3 benchmarks/bench.py     # reproduce the savings table
```
