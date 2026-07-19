"""Content-addressed reversible store.

Every lossy transform in slimctx writes the *original* content here before
mutating anything, and embeds a marker in the compressed output:

    [slimctx-ref sha=<hash> tokens=<n>] ...compressed content...

An agent (or the `retrieve` MCP tool) can fetch the byte-exact original at
any time. This invariant holds for every content type — JSON, text, logs,
and code alike.

Backends:
  * MemoryStore  — bounded LRU with TTL, for single-process use / tests.
  * SqliteStore  — persistent, WAL-mode, safe for concurrent workers.

Encryption: pass `cipher=(encrypt_fn, decrypt_fn)` to SqliteStore to
transparently encrypt payloads at rest (e.g. cryptography.Fernet). The
store itself stays dependency-free; the hook lets deployments opt in.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional, Protocol, Tuple

DEFAULT_TTL_SECONDS = 6 * 3600  # generous: retrieval may happen much later
HASH_LEN = 24  # 96 bits of a sha256 — ample for a bounded local store

# refs come back from *model output* — treat them as untrusted input and
# reject anything that is not exactly a lowercase-hex ref before it touches
# a backend.
_REF_RE = re.compile(r"^[0-9a-f]{%d}$" % HASH_LEN)


def content_hash(payload: str, salt: str = "") -> str:
    """Content hash, optionally salted.

    A salt prevents cross-tenant probing on shared stores: without one,
    anyone who can call get() can test whether a *known* payload is in the
    store by computing its hash. Single-agent local deployments don't need
    it; shared stores should set one.
    """
    h = hashlib.sha256()
    if salt:
        h.update(salt.encode("utf-8"))
        h.update(b"\x00")
    h.update(payload.encode("utf-8"))
    return h.hexdigest()[:HASH_LEN]


def valid_ref(ref: str) -> bool:
    return isinstance(ref, str) and bool(_REF_RE.match(ref))


class Store(Protocol):
    def put(self, payload: str) -> str:
        """Store payload, return its content hash."""
        ...

    def get(self, ref: str) -> Optional[str]:
        """Return the original payload for ref, or None if expired/unknown."""
        ...


class MemoryStore:
    """Bounded LRU + TTL store. Thread-safe."""

    def __init__(
        self,
        capacity: int = 4096,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        salt: str = "",
    ):
        self._data: OrderedDict[str, Tuple[float, str]] = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._salt = salt
        self._lock = threading.Lock()

    def put(self, payload: str) -> str:
        ref = content_hash(payload, self._salt)
        now = time.monotonic()
        with self._lock:
            self._data[ref] = (now, payload)
            self._data.move_to_end(ref)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)
        return ref

    def get(self, ref: str) -> Optional[str]:
        if not valid_ref(ref):
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(ref)
            if entry is None:
                return None
            stored_at, payload = entry
            if now - stored_at > self._ttl:
                del self._data[ref]
                return None
            self._data.move_to_end(ref)
            return payload

    def __len__(self) -> int:
        return len(self._data)


class SqliteStore:
    """Persistent store; survives process restarts, safe across workers."""

    def __init__(
        self,
        path: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        cipher: Optional[Tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]] = None,
        salt: str = "",
    ):
        self._path = path
        self._ttl = ttl_seconds
        self._encrypt, self._decrypt = cipher if cipher else (None, None)
        self._salt = salt
        self._local = threading.local()
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                " ref TEXT PRIMARY KEY,"
                " payload BLOB NOT NULL,"
                " stored_at REAL NOT NULL)"
            )
            conn.execute("PRAGMA journal_mode=WAL")
        # The store holds originals of everything that flowed through the
        # agent — restrict to owner even if the process umask is loose.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(path + suffix, 0o600)
            except OSError:
                pass

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path)
            self._local.conn = conn
        return conn

    def put(self, payload: str) -> str:
        ref = content_hash(payload, self._salt)
        blob = payload.encode("utf-8")
        if self._encrypt:
            blob = self._encrypt(blob)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entries (ref, payload, stored_at) VALUES (?, ?, ?)",
                (ref, blob, time.time()),
            )
        return ref

    def get(self, ref: str) -> Optional[str]:
        if not valid_ref(ref):
            return None
        conn = self._conn()
        cutoff = time.time() - self._ttl
        # Lazy purge keeps the file bounded without a background thread.
        with conn:
            conn.execute("DELETE FROM entries WHERE stored_at < ?", (cutoff,))
        row = conn.execute(
            "SELECT payload FROM entries WHERE ref = ?", (ref,)
        ).fetchone()
        if row is None:
            return None
        blob = row[0]
        if self._decrypt:
            blob = self._decrypt(blob)
        return blob.decode("utf-8")


def make_marker(ref: str, original_tokens: int, kept_desc: str) -> str:
    """The inline marker that tells the model how to get the original back."""
    return (
        f"[slimctx-ref {ref} original~{original_tokens}tok {kept_desc}; "
        f"call retrieve('{ref}') for full content]"
    )
