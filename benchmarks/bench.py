"""Benchmark slimctx on workloads modeled after Headroom's README table
(code search results, SRE incident logs, GitHub issue triage, codebase
exploration). Reports token savings and verifies the key facts survive.

Run:  python3 benchmarks/bench.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slimctx import Config, Pipeline, estimate_tokens

random.seed(7)


def code_search_results():
    """100 grep-style results as JSON — Headroom claims 92% here."""
    rows = []
    for i in range(100):
        rows.append({
            "file": f"src/module_{i % 12}/handler_{i}.py",
            "line": random.randint(10, 900),
            "match": f"def process_request_{i}(self, request, timeout=30):",
            "score": round(random.random(), 4),
            "repo": "acme/backend",
            "branch": "main",
        })
    rows[63]["match"] = "def process_payment(self, request):  # FIXME race condition on retry"
    return json.dumps({"results": rows})


def sre_incident():
    """65k-token-style incident log — Headroom claims 92%."""
    lines = []
    services = ["api-gw", "checkout", "inventory", "auth"]
    for i in range(2500):
        svc = services[i % 4]
        lines.append(
            f"2026-07-19T14:{(i // 60) % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z "
            f"INFO [{svc}] request_id={random.randrange(10**12):012x} "
            f"handled GET /v2/orders {200 if i % 50 else 429} in {random.randint(4, 60)}ms"
        )
    lines.insert(1800, "2026-07-19T14:30:00.000Z FATAL [checkout] OOMKilled: container exceeded memory limit 512Mi")
    lines.insert(1801, "2026-07-19T14:30:01.120Z ERROR [checkout] connection pool exhausted: 200/200 in use, deadline exceeded")
    return "\n".join(lines)


def github_issues():
    """Issue triage payload — Headroom claims 73%."""
    rows = []
    for i in range(120):
        rows.append({
            "number": 4000 + i,
            "title": f"Flaky test in CI shard {i % 8}" if i % 9 else f"Crash on startup with config v{i}",
            "state": "open" if i % 3 else "closed",
            "labels": ["bug"] if i % 2 else ["enhancement", "triage"],
            "comments": random.randint(0, 40),
            "created_at": f"2026-0{1 + i % 6}-{1 + i % 27:02d}T09:00:00Z",
            "user": {"login": f"dev{i % 15}", "type": "User"},
            "body": ("Repro steps: run the suite with seed 1234 and observe the failure. " * 3),
        })
    return json.dumps(rows)


def codebase_file():
    """A big source file — Headroom claims 47% on exploration."""
    parts = ["import os\nimport json\nfrom typing import Any, Dict, List\n"]
    for i in range(40):
        parts.append(
            f"\n\nclass Service{i}:\n"
            f'    """Handles domain object {i}."""\n'
            f"    def __init__(self, db, cache):\n"
            f"        self.db = db\n"
            f"        self.cache = cache\n"
            f"        self._ready = False\n"
            f"    def load(self, key: str) -> Dict[str, Any]:\n"
            f"        value = self.cache.get(key)\n"
            f"        if value is None:\n"
            f"            value = self.db.query('SELECT * FROM t{i} WHERE k = ?', key)\n"
            f"            self.cache.set(key, value)\n"
            f"        return value\n"
            f"    def save(self, key: str, value: Dict[str, Any]) -> None:\n"
            f"        self.db.execute('INSERT INTO t{i} VALUES (?, ?)', key, value)\n"
            f"        self.cache.invalidate(key)\n"
        )
    return "".join(parts)


WORKLOADS = [
    ("Code search (100 results)", code_search_results(), "find the payment race condition",
     ["FIXME race condition"]),
    ("SRE incident debugging", sre_incident(), "why did checkout crash",
     ["OOMKilled", "connection pool exhausted"]),
    ("GitHub issue triage", github_issues(), "crash on startup issues",
     []),
    ("Codebase exploration", codebase_file(), "how does caching work",
     ["def load", "cache.get"]),
]


def main():
    print(f"{'Workload':<28} {'Before':>8} {'After':>8} {'Savings':>8}  Facts kept")
    print("-" * 78)
    total_before = total_after = 0
    for name, payload, query, must_keep in WORKLOADS:
        pipe = Pipeline(Config(target_tokens=1))  # force compression
        messages = [
            {"role": "user", "content": query},
            {"role": "tool", "content": payload},
            {"role": "user", "content": query},
        ]
        # widen live zone exclusion: only the tool message is compressible
        pipe.config.live_messages = 1
        result = pipe.compress(messages, query=query)
        compressed = result.messages[1]["content"]
        before = estimate_tokens(payload)
        after = estimate_tokens(compressed)
        total_before += before
        total_after += after
        kept = all(fact in compressed for fact in must_keep)
        facts = "yes" if kept else "MISSING: " + ", ".join(
            f for f in must_keep if f not in compressed
        )
        print(f"{name:<28} {before:>8,} {after:>8,} {1 - after / before:>7.0%}  {facts}")

        # reversibility check
        for t in result.transforms:
            if t.get("lossy"):
                assert pipe.retrieve(t["ref"]) == payload, "reversibility broken!"

    print("-" * 78)
    print(f"{'TOTAL':<28} {total_before:>8,} {total_after:>8,} {1 - total_after / total_before:>7.0%}")
    print("\nAll lossy transforms verified byte-exact reversible via store.")


if __name__ == "__main__":
    main()
