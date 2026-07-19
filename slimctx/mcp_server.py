"""Minimal MCP (Model Context Protocol) server over stdio — stdlib only.

Exposes slimctx to any MCP client (GitHub Copilot agent mode in VS Code,
Claude Code, Cursor, ...) as three tools:

  * slimctx_compress  — compress a blob of text/JSON/logs/code
  * slimctx_retrieve  — fetch the byte-exact original for a ref
  * slimctx_stats     — session savings counters

Run:  python -m slimctx.mcp_server [--db PATH] [--target-tokens N]

The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, per the
MCP stdio spec. No network socket is opened — the client owns the process.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from .pipeline import Config, Pipeline
from .store import SqliteStore
from .tokens import estimate_tokens

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "slimctx_compress",
        "description": (
            "Compress large tool output, logs, JSON, or file content before "
            "reasoning over it. Returns a compact version that preserves "
            "errors, outliers, and query-relevant content. If the result "
            "contains a [slimctx-ref ...] marker, the full original can be "
            "recovered with slimctx_retrieve."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Content to compress"},
                "query": {
                    "type": "string",
                    "description": "What you are looking for (steers relevance)",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "slimctx_retrieve",
        "description": (
            "Recover the byte-exact original content for a [slimctx-ref <ref>] "
            "marker seen in previously compressed output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "24-char hex ref from a marker"}
            },
            "required": ["ref"],
        },
    },
    {
        "name": "slimctx_stats",
        "description": "Token savings accumulated in this session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self.tokens_in = 0
        self.tokens_out = 0

    # -- tool implementations ------------------------------------------------

    def compress(self, text: str, query: str = "") -> str:
        before = estimate_tokens(text)
        out = self.pipeline.compress_blob(text, query=query)
        self.tokens_in += before
        self.tokens_out += estimate_tokens(out)
        return out

    def retrieve(self, ref: str) -> str:
        original = self.pipeline.retrieve(ref)
        if original is None:
            return f"[slimctx] no content for ref {ref!r} (expired, evicted, or invalid)"
        return original

    def stats(self) -> str:
        saved = self.tokens_in - self.tokens_out
        pct = (saved / self.tokens_in * 100) if self.tokens_in else 0.0
        return (
            f"tokens in: {self.tokens_in:,} | out: {self.tokens_out:,} | "
            f"saved: {saved:,} ({pct:.0f}%)"
        )

    # -- JSON-RPC plumbing ---------------------------------------------------

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if method == "initialize":
            return self._result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "slimctx", "version": "0.1.0"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None  # notifications get no response
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(msg_id, msg.get("params") or {})
        if msg_id is not None:
            return self._error(msg_id, -32601, f"method not found: {method}")
        return None

    def _call_tool(self, msg_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "slimctx_compress":
                text = self.compress(str(args.get("text", "")), str(args.get("query", "")))
            elif name == "slimctx_retrieve":
                text = self.retrieve(str(args.get("ref", "")))
            elif name == "slimctx_stats":
                text = self.stats()
            else:
                return self._error(msg_id, -32602, f"unknown tool: {name}")
        except Exception as exc:  # tool errors go back in-band, never crash
            return self._result(msg_id, {
                "content": [{"type": "text", "text": f"[slimctx] error: {exc}"}],
                "isError": True,
            })
        return self._result(msg_id, {"content": [{"type": "text", "text": text}]})

    @staticmethod
    def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main() -> None:
    parser = argparse.ArgumentParser(description="slimctx MCP server (stdio)")
    parser.add_argument("--db", help="SQLite path for the reversible store "
                        "(default: in-memory, lost on exit)")
    parser.add_argument("--target-tokens", type=int, default=32_000)
    parser.add_argument("--salt", default="", help="salt for store refs "
                        "(recommended when the db file is shared)")
    args = parser.parse_args()

    store = SqliteStore(args.db, salt=args.salt) if args.db else None
    pipeline = Pipeline(Config(target_tokens=args.target_tokens), store=store)
    server = Server(pipeline)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
