"""Measure each test's CPU under the selector's own harness (inline pools).

Usage: python cost_tests.py /data/tests.json /out/costs.json JOBS
"""

import json
import os
import resource
import sys
import time

sys.path.insert(0, "/app")
os.environ["SKIP_INTEGRATION"] = "0"


def child(test, out_fd):
    import pytest
    import verify

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    verify._inline_pools()
    import signal
    signal.alarm(120)  # a pathological test must not stall the run
    t0 = resource.getrusage(resource.RUSAGE_SELF)
    w0 = time.monotonic()
    rc = pytest.main(["-q", "-p", "no:cacheprovider", "-o", "addopts=", "--no-header",
                      "-W", "ignore", test["id"]])
    t1 = resource.getrusage(resource.RUSAGE_SELF)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (t1.ru_utime - t0.ru_utime) + (t1.ru_stime - t0.ru_stime) + c1.ru_utime + c1.ru_stime
    os.write(out_fd, (json.dumps({"n": test["number"], "cpu": round(cpu, 3), "rc": int(rc),
                                  "wall": round(time.monotonic() - w0, 3)}) + "\n").encode())


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    max_s = float(os.environ.get("MAX_S", "1.0"))
    tests = [t for t in tests if t["seconds"] < max_s]  # giants are never run by us
    jobs = int(sys.argv[3])
    os.chdir("/sqlglot")
    sys.path.insert(0, "/sqlglot")
    import pytest  # noqa: F401  (before sqlglot, as in the selector)
    import verify

    verify.preload()
    out_fd = os.open(sys.argv[2] + ".jsonl", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
    running = {}
    queue = sorted(tests, key=lambda t: -t["seconds"])
    while queue or running:
        while queue and len(running) < jobs:
            t = queue.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    child(t, out_fd)
                finally:
                    os._exit(0)
            running[pid] = t
        pid, _ = os.wait()
        running.pop(pid, None)
    res = {}
    for line in open(sys.argv[2] + ".jsonl"):
        r = json.loads(line)
        res[r["n"]] = r
    json.dump(res, open(sys.argv[2], "w"))
    bad = [n for n, r in res.items() if r["rc"] != 0]
    print(f"measured {len(res)} tests, failing at base: {bad}", file=sys.stderr)


if __name__ == "__main__":
    main()
