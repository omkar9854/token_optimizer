"""The pipeline: compress a message list against a token budget.

Design rules (each fixes a real failure mode in tools like this):

1. LIVE ZONE — the system prompt and the last `live_messages` turns are
   never touched. Recent context is what the model is actively reasoning
   over; compressing it degrades answers far more than it saves.

2. UNIVERSAL REVERSIBILITY — before any lossy transform, the original
   blob goes into the store and the output carries a [slimctx-ref ...]
   marker. text/log/code/json alike. Nothing is ever unrecoverable.

3. PREFIX STABILITY — messages are compressed once and the result is
   cached by content hash. Re-running the pipeline over a growing
   conversation re-emits byte-identical compressed prefixes, so provider
   KV/prompt caches keep hitting. (Compressing the same message
   differently on each request silently destroys prompt caching; here
   stability falls out of determinism plus memoization.)

4. NET-GAIN GUARD — if a transform saves fewer than `min_saving_tokens`,
   the original is kept. Marker overhead must never exceed the win.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .code_compressor import compress_code
from .json_compressor import tabularize
from .log_compressor import compress_log
from .router import ContentType, detect
from .store import MemoryStore, Store, make_marker
from .text_compressor import compress_text
from .tokens import estimate_tokens


@dataclass
class Config:
    target_tokens: int = 32_000          # compress until under this budget
    live_messages: int = 4               # never touch the last N messages
    min_compress_tokens: int = 256       # blobs smaller than this pass through
    min_saving_tokens: int = 48          # transform must save at least this
    text_ratio: float = 0.4              # extractive keep-ratio for prose
    json_max_rows: int = 30              # lossy row cap for large JSON arrays
    max_blob_bytes: int = 8_000_000      # refuse to analyze larger blobs
    memo_capacity: int = 2048            # bound on the determinism memo


@dataclass
class Result:
    messages: List[Dict[str, Any]]
    tokens_before: int
    tokens_after: int
    transforms: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def savings_ratio(self) -> float:
        if self.tokens_before == 0:
            return 0.0
        return 1.0 - self.tokens_after / self.tokens_before


class Pipeline:
    def __init__(self, config: Optional[Config] = None, store: Optional[Store] = None):
        self.config = config or Config()
        self.store = store or MemoryStore()
        # content-hash -> (compressed, info); bounded FIFO (rule 3)
        self._memo: "OrderedDict[str, Tuple[str, Dict[str, Any]]]" = OrderedDict()

    # -- public API ---------------------------------------------------------

    def compress(self, messages: List[Dict[str, Any]], query: str = "") -> Result:
        """Compress OpenAI/Anthropic-style messages ({role, content} dicts).

        `query` (defaults to the last user message) steers relevance
        scoring so content related to what the user asked survives.
        """
        cfg = self.config
        before = sum(estimate_tokens(_content_str(m)) for m in messages)
        if not query:
            query = _last_user_text(messages)

        result_msgs = [dict(m) for m in messages]
        transforms: List[Dict[str, Any]] = []

        if before > cfg.target_tokens:
            protected = self._protected_indices(result_msgs)
            # Compress oldest-and-largest first: sort candidates by size desc,
            # tie-broken by age, and stop as soon as we're under budget.
            candidates = [
                (i, estimate_tokens(_content_str(m)))
                for i, m in enumerate(result_msgs)
                if i not in protected
                and isinstance(m.get("content"), str)
                and estimate_tokens(m["content"]) >= cfg.min_compress_tokens
            ]
            candidates.sort(key=lambda t: (-t[1], t[0]))

            running = before
            for i, size in candidates:
                if running <= cfg.target_tokens:
                    break
                original = result_msgs[i]["content"]
                compressed, info = self._compress_blob(original, query)
                if compressed is None:
                    continue
                saved = size - estimate_tokens(compressed)
                if saved < cfg.min_saving_tokens:
                    continue
                result_msgs[i]["content"] = compressed
                running -= saved
                info.update({"message_index": i, "tokens_saved": saved})
                transforms.append(info)

        after = sum(estimate_tokens(_content_str(m)) for m in result_msgs)
        return Result(result_msgs, before, after, transforms)

    def compress_blob(self, text: str, query: str = "") -> str:
        """Compress a single string (for MCP-tool style usage)."""
        compressed, _ = self._compress_blob(text, query)
        return compressed if compressed is not None else text

    def retrieve(self, ref: str) -> Optional[str]:
        """Fetch the byte-exact original for a [slimctx-ref ...] marker."""
        return self.store.get(ref)

    # -- internals ----------------------------------------------------------

    def _protected_indices(self, messages: List[Dict[str, Any]]) -> set:
        protected = {i for i, m in enumerate(messages) if m.get("role") == "system"}
        n = len(messages)
        protected |= set(range(max(0, n - self.config.live_messages), n))
        return protected

    def _compress_blob(self, text: str, query: str):
        from .store import content_hash  # local import to avoid cycle noise

        # DoS guard: a single hostile multi-megabyte blob must not pin the
        # CPU in parsers/regexes. Anything this size blows any real context
        # window anyway — pass it through untouched and let the caller's
        # provider reject it.
        if len(text) > self.config.max_blob_bytes:
            return None, {}

        memo_key = content_hash(text + "\x00" + query)
        if memo_key in self._memo:
            compressed, info = self._memo[memo_key]
            return compressed, dict(info)

        ctype = detect(text)
        original_tokens = estimate_tokens(text)
        cfg = self.config

        compressed: Optional[str] = None
        lossy = False
        if ctype is ContentType.JSON:
            out = tabularize(text, query=query, max_rows=cfg.json_max_rows)
            if out is not None:
                compressed, lossy = out
        elif ctype is ContentType.LOG:
            compressed, lossy = compress_log(text), True
        elif ctype is ContentType.CODE:
            out = compress_code(text, query=query)
            if out is not None:
                compressed, lossy = out, True
        else:
            compressed, lossy = compress_text(text, query=query, target_ratio=cfg.text_ratio), True
            if compressed == text:
                compressed = None

        if compressed is None:
            return None, {}

        if lossy:
            ref = self.store.put(text)
            kept_desc = f"type={ctype.value}"
            compressed = make_marker(ref, original_tokens, kept_desc) + "\n" + compressed
        else:
            ref = None

        info = {"content_type": ctype.value, "ref": ref, "lossy": lossy}
        self._memo[memo_key] = (compressed, info)
        while len(self._memo) > self.config.memo_capacity:
            self._memo.popitem(last=False)
        return compressed, dict(info)


def _content_str(message: Dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    # Anthropic-style content blocks
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return str(content)


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return _content_str(m)
    return ""
