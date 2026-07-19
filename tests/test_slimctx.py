"""Tests for the guarantees slimctx makes.

The important ones are invariants, not examples:
  * reversibility  — every lossy transform's original is retrievable
  * error safety   — no compressor ever drops an error line
  * determinism    — same input -> byte-identical output (cache stability)
  * net gain       — compression never increases token count
"""

import json
import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slimctx import Config, Pipeline, estimate_tokens
from slimctx.json_compressor import tabularize
from slimctx.log_compressor import compress_log, looks_like_log
from slimctx.router import ContentType, detect
from slimctx.store import MemoryStore, SqliteStore
from slimctx.text_compressor import compress_text


random.seed(42)


def make_json_payload(n=200):
    rows = [
        {
            "id": i,
            "name": f"user-{i}",
            "status": "active" if i % 7 else "suspended",
            "latency_ms": random.randint(5, 40) if i != 117 else 9500,
            "meta": {"region": "us-east-1", "tier": "standard"},
        }
        for i in range(n)
    ]
    rows[min(50, n - 1)]["error"] = "connection timeout to shard 3"
    return json.dumps({"items": rows})


def make_log_payload(n=800):
    lines = []
    for i in range(n):
        lines.append(f"2026-07-19T10:{i % 60:02d}:{i % 60:02d}Z INFO GET /api/users/{1000 + i} 200 {random.randint(3, 30)}ms")
        if i == 400:
            lines.append("2026-07-19T10:40:00Z ERROR upstream connection refused to db-primary:5432")
    return "\n".join(lines)


PROSE = (
    "The deployment pipeline consists of four stages. "
    "First, code is linted and unit tests are executed on every commit. "
    "Second, integration tests run against a staging database with production-like data. "
    "The staging environment mirrors production topology exactly. "
    "Third, a canary deployment routes five percent of traffic to the new version. "
    "Metrics are monitored for fifteen minutes during the canary phase. "
    "If error rates exceed baseline by two percent, the deployment is rolled back automatically. "
    "Fourth, the full rollout proceeds region by region. "
    "Each region completes before the next one begins. "
    "A failed health check in any region triggers an immediate halt. "
    "The whole process typically takes ninety minutes end to end. "
    "Rollbacks have a fatal flaw: they do not restore database migrations."
)


# ---------------------------------------------------------------- routing

def test_router_detects_types():
    assert detect(make_json_payload()) is ContentType.JSON
    assert detect(make_log_payload()) is ContentType.LOG
    assert detect(PROSE) is ContentType.TEXT


# ---------------------------------------------------------------- stores

def test_memory_store_roundtrip():
    store = MemoryStore()
    payload = make_log_payload()
    ref = store.put(payload)
    assert store.get(ref) == payload


def test_memory_store_lru_bound():
    store = MemoryStore(capacity=10)
    for i in range(50):
        store.put(f"payload-{i}")
    assert len(store) == 10


def test_sqlite_store_roundtrip(tmp_path):
    store = SqliteStore(str(tmp_path / "ccr.db"))
    payload = make_json_payload()
    ref = store.put(payload)
    assert store.get(ref) == payload
    # re-open: persistence across "restarts"
    store2 = SqliteStore(str(tmp_path / "ccr.db"))
    assert store2.get(ref) == payload


def test_sqlite_store_cipher_hook(tmp_path):
    key = 0x5A
    cipher = (
        lambda b: bytes(x ^ key for x in b),
        lambda b: bytes(x ^ key for x in b),
    )
    store = SqliteStore(str(tmp_path / "enc.db"), cipher=cipher)
    ref = store.put("secret payload")
    assert store.get(ref) == "secret payload"
    # raw bytes on disk must not contain the plaintext
    raw = (tmp_path / "enc.db").read_bytes()
    assert b"secret payload" not in raw


# ---------------------------------------------------------------- JSON

def test_json_lossless_tabularize_keeps_all_rows():
    payload = make_json_payload(50)
    out, lossy = tabularize(payload, max_rows=None)
    assert not lossy
    for i in range(50):
        assert f"user-{i}" in out
    assert estimate_tokens(out) < estimate_tokens(payload) * 0.6


def test_json_lossy_keeps_errors_and_outliers():
    payload = make_json_payload(200)
    out, lossy = tabularize(payload, max_rows=25)
    assert lossy
    assert "connection timeout to shard 3" in out   # salient row pinned
    assert "9500" in out                            # numeric outlier pinned
    assert "user-0" in out and "user-199" in out    # head/tail anchors


def test_json_query_relevance_pins_matching_row():
    payload = make_json_payload(200)
    out, _ = tabularize(payload, query="what happened to user-143", max_rows=20)
    assert "user-143" in out


# ---------------------------------------------------------------- logs

def test_log_collapse_keeps_error_verbatim():
    payload = make_log_payload()
    assert looks_like_log(payload)
    out = compress_log(payload)
    assert "ERROR upstream connection refused to db-primary:5432" in out
    assert estimate_tokens(out) < estimate_tokens(payload) * 0.15


def test_log_template_counts():
    payload = make_log_payload(500)
    out = compress_log(payload)
    assert "[x" in out  # collapsed template with count


# ---------------------------------------------------------------- text

def test_text_keeps_salient_sentence():
    out = compress_text(PROSE, target_ratio=0.3)
    assert "fatal flaw" in out
    assert len(out) < len(PROSE)


def test_text_query_steers_selection():
    out = compress_text(PROSE, query="canary deployment traffic", target_ratio=0.25)
    assert "canary" in out.lower()


# ---------------------------------------------------------------- pipeline invariants

def _build_conversation():
    return [
        {"role": "system", "content": "You are a helpful SRE assistant."},
        {"role": "user", "content": "Why is checkout latency spiking?"},
        {"role": "assistant", "content": "Let me check the service logs."},
        {"role": "tool", "content": make_log_payload()},
        {"role": "assistant", "content": "Now checking user records."},
        {"role": "tool", "content": make_json_payload()},
        {"role": "user", "content": "Focus on the database errors please."},
        {"role": "assistant", "content": "Looking into db-primary now."},
    ]


def test_pipeline_reversibility_invariant():
    """Every lossy transform must leave a retrievable, byte-exact original."""
    pipe = Pipeline(Config(target_tokens=500, live_messages=2))
    messages = _build_conversation()
    originals = {i: m["content"] for i, m in enumerate(messages)}
    result = pipe.compress(messages)
    assert result.transforms, "expected at least one transform"
    for t in result.transforms:
        if t.get("lossy"):
            assert t["ref"] is not None
            assert pipe.retrieve(t["ref"]) == originals[t["message_index"]]


def test_pipeline_never_grows_tokens():
    pipe = Pipeline(Config(target_tokens=500, live_messages=2))
    result = pipe.compress(_build_conversation())
    assert result.tokens_after <= result.tokens_before


def test_pipeline_protects_live_zone_and_system():
    pipe = Pipeline(Config(target_tokens=100, live_messages=2))
    messages = _build_conversation()
    result = pipe.compress(messages)
    assert result.messages[0]["content"] == messages[0]["content"]   # system
    assert result.messages[-1]["content"] == messages[-1]["content"]  # live
    assert result.messages[-2]["content"] == messages[-2]["content"]  # live


def test_pipeline_deterministic_for_cache_stability():
    """Same input twice -> byte-identical output (provider caches depend on it)."""
    pipe = Pipeline(Config(target_tokens=500, live_messages=2))
    messages = _build_conversation()
    a = pipe.compress([dict(m) for m in messages])
    b = pipe.compress([dict(m) for m in messages])
    assert [m["content"] for m in a.messages] == [m["content"] for m in b.messages]

    # And across independent pipeline instances (no hidden per-process state).
    pipe2 = Pipeline(Config(target_tokens=500, live_messages=2))
    c = pipe2.compress([dict(m) for m in messages])
    assert [m["content"] for m in a.messages] == [m["content"] for m in c.messages]


def test_pipeline_under_budget_is_untouched():
    pipe = Pipeline(Config(target_tokens=10_000_000))
    messages = _build_conversation()
    result = pipe.compress(messages)
    assert [m["content"] for m in result.messages] == [m["content"] for m in messages]
    assert result.transforms == []


# ---------------------------------------------------------------- code

def test_code_skeleton_keeps_query_relevant_body():
    from slimctx.code_compressor import compress_code
    body_filler = "\n".join(f"        step_{j} = self.db.op({j})" for j in range(8))
    src = "\n".join(
        f"class S{i}:\n"
        f"    def load(self, k):\n"
        f"        v = self.cache.get(k)\n"
        f"        return v\n"
        f"    def save(self, k, v):\n"
        f"{body_filler}\n"
        f"        self.db.write(k, v)\n"
        for i in range(10)
    )
    out = compress_code(src, query="how does caching work")
    assert out is not None
    assert "cache.get" in out           # relevant body kept verbatim
    assert "... #" in out               # others elided


# ---------------------------------------------------------------- security

def test_store_rejects_malformed_refs():
    """Refs arrive from model output — anything non-hex must be refused
    before touching a backend."""
    store = MemoryStore()
    store.put("data")
    for bad in ("../../etc/passwd", "'; DROP TABLE entries;--", "A" * 24,
                "abc", "", "deadbeef" * 10, None):
        try:
            assert store.get(bad) is None
        except TypeError:
            pass  # non-str rejected outright is also acceptable


def test_sqlite_store_rejects_malformed_refs(tmp_path):
    store = SqliteStore(str(tmp_path / "sec.db"))
    store.put("data")
    assert store.get("'; DROP TABLE entries;--") is None
    assert store.get("../../../etc/passwd") is None


def test_sqlite_store_file_permissions(tmp_path):
    import stat
    path = tmp_path / "perm.db"
    SqliteStore(str(path))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_salted_store_changes_refs():
    plain = MemoryStore()
    salted = MemoryStore(salt="tenant-a")
    assert plain.put("same payload") != salted.put("same payload")
    # each store still round-trips its own ref
    assert salted.get(salted.put("same payload")) == "same payload"


def test_hostile_deeply_nested_json_does_not_crash():
    from slimctx.json_compressor import try_parse
    bomb = "[" * 200_000  # unterminated + pathologically nested
    assert try_parse(bomb) is None
    bomb2 = "[" * 100_000 + "]" * 100_000  # valid but absurdly deep
    assert try_parse(bomb2) is None or True  # must not raise


def test_oversized_blob_passes_through_untouched():
    pipe = Pipeline(Config(target_tokens=1, live_messages=0, max_blob_bytes=1000))
    big = "x " * 5000  # > 1000 bytes
    messages = [{"role": "tool", "content": big}]
    result = pipe.compress(messages)
    assert result.messages[0]["content"] == big


def test_memo_is_bounded():
    pipe = Pipeline(Config(target_tokens=1, live_messages=0, memo_capacity=5))
    for i in range(30):
        pipe.compress([{"role": "tool", "content": f"blob {i} " + PROSE}])
    assert len(pipe._memo) <= 5


def test_memoized_transform_keeps_ref_info():
    """Second compression of the same content must report the same ref,
    not lose it (transform accounting feeds retrieval)."""
    pipe = Pipeline(Config(target_tokens=1, live_messages=0))
    msgs = [{"role": "tool", "content": make_log_payload()}]
    r1 = pipe.compress([dict(m) for m in msgs])
    r2 = pipe.compress([dict(m) for m in msgs])
    lossy1 = [t for t in r1.transforms if t.get("lossy")]
    lossy2 = [t for t in r2.transforms if t.get("lossy")]
    assert lossy1 and lossy2
    assert lossy1[0]["ref"] == lossy2[0]["ref"]
    assert pipe.retrieve(lossy2[0]["ref"]) == msgs[0]["content"]
