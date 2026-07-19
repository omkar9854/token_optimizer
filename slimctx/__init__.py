"""slimctx — a zero-dependency, fully-reversible context compression layer
for AI agents.

    from slimctx import Pipeline, Config

    pipe = Pipeline(Config(target_tokens=32_000))
    result = pipe.compress(messages)          # OpenAI/Anthropic style dicts
    result.savings_ratio                      # e.g. 0.72
    pipe.retrieve("a1b2c3...")                # byte-exact original back

Guarantees:
  * pure Python stdlib — no models, no downloads, no network, ever
  * every lossy transform stores the original first (always reversible)
  * deterministic + memoized -> stable prefixes -> provider caches hit
  * error/warning content is never dropped by any compressor
"""

from .pipeline import Config, Pipeline, Result
from .store import MemoryStore, SqliteStore
from .tokens import estimate_tokens

__version__ = "0.1.0"
__all__ = [
    "Pipeline",
    "Config",
    "Result",
    "MemoryStore",
    "SqliteStore",
    "estimate_tokens",
]
