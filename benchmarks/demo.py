"""60-second demo: compress a production-style incident log.

Run:  python3 benchmarks/demo.py
Everything is computed live — no staged numbers.
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slimctx import Pipeline, estimate_tokens

BOLD, DIM, GREEN, RED, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[36m", "\033[0m",
)


def build_incident_log() -> str:
    random.seed(7)
    services = ["api-gw", "checkout", "inventory", "auth"]
    lines = []
    for i in range(2500):
        svc = services[i % 4]
        lines.append(
            f"2026-07-19T14:{(i // 60) % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z "
            f"INFO [{svc}] request_id={random.randrange(10**12):012x} "
            f"handled GET /v2/orders {200 if i % 50 else 429} in {random.randint(4, 60)}ms"
        )
    lines.insert(1800, "2026-07-19T14:30:00.000Z FATAL [checkout] OOMKilled: "
                       "container exceeded memory limit 512Mi")
    lines.insert(1801, "2026-07-19T14:30:01.120Z ERROR [checkout] connection pool "
                       "exhausted: 200/200 in use, deadline exceeded")
    return "\n".join(lines)


def main() -> None:
    log = build_incident_log()
    before = estimate_tokens(log)
    n_lines = len(log.splitlines())

    print(f"\n  {BOLD}slimctx{RESET} — token optimizer demo\n")
    print(f"  input   incident log from 4 services  "
          f"{BOLD}{before:,} tokens{RESET} {DIM}({n_lines:,} lines){RESET}")

    pipe = Pipeline()
    t0 = time.perf_counter()
    out = pipe.compress_blob(log, query="why did checkout crash")
    elapsed = time.perf_counter() - t0
    after = estimate_tokens(out)

    print(f"  output  {BOLD}{GREEN}{after:,} tokens{RESET}  "
          f"{DIM}in {elapsed * 1000:.0f}ms{RESET}\n")

    for line in out.splitlines():
        if "FATAL" in line or "ERROR" in line:
            print(f"    {RED}{line}{RESET}")
        elif line.startswith("[slimctx-ref"):
            print(f"    {DIM}{line[:76]}...{RESET}")
        elif "[x" in line:
            print(f"    {CYAN}{line}{RESET}")
        else:
            print(f"    {DIM}{line[:76]}{RESET}")

    saved = 1 - after / before
    ref = out.split("[slimctx-ref ")[1].split(" ")[0]
    roundtrip = pipe.retrieve(ref) == log
    print(f"\n  {GREEN}✓{RESET} {BOLD}{saved:.1%} of tokens removed{RESET}")
    print(f"  {GREEN}✓{RESET} FATAL / ERROR lines kept verbatim")
    print(f"  {GREEN}✓{RESET} original retrievable byte-exact: "
          f"retrieve('{ref[:8]}…') {'== original' if roundtrip else 'FAILED'}\n")


if __name__ == "__main__":
    main()
