"""Per-test tracing at the pinned commit (offline, run in the lab image).

The parent imports every sqlglot module and every test module once, then forks
one child per test. Each child records:
  * runtime line coverage of sqlglot/ (import-time lines excluded: parent did them)
  * files under the repo opened during the test (fixtures)
  * pass/fail and CPU seconds

Usage: python trace_tests.py /data/tests.json /out/trace.jsonl [jobs]
"""

import importlib
import json
import os
import pkgutil
import resource
import sys
import time

ROOT = "/sqlglot"
os.environ.setdefault("BR_TRACE_DIR", "/out/sub")
os.environ.setdefault("COVERAGE_CORE", "sysmon")


def preimport() -> None:
    sys.path.insert(0, ROOT)
    # Only what every test imports anyway: importing *all* modules changes
    # global state (breaks test_parser::test_expression).
    # pytest MUST be imported first: sqlglot/expressions/core.py sets
    # UNITTEST = "unittest" in sys.modules or "pytest" in sys.modules at import.
    import pytest  # noqa
    import sqlglot  # noqa


def child(test: dict, out_fd: int) -> None:
    import coverage
    import pytest

    opened: set[str] = set()
    os.environ["BR_TEST"] = str(test["number"])

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            p = os.fsdecode(args[0])
            if not os.path.isabs(p):
                p = os.path.join(os.getcwd(), p)
            p = os.path.normpath(p)
            if p.startswith(ROOT + "/") and "/__pycache__/" not in p and not p.endswith(".pyc"):
                opened.add(p[len(ROOT) + 1 :])

    cov = coverage.Coverage(
        data_file=None, include=[ROOT + "/sqlglot/*"], config_file=False
    )
    sys.addaudithook(audit)
    sys.path.insert(0, "/tools")
    import br_classes

    br_classes.start()
    cov.start()
    t0 = time.process_time()
    rc = pytest.main(
        ["-q", "-p", "no:cacheprovider", "-p", "no:randomly", "--no-header", "-o",
         "addopts=", test["id"]],
    )
    cpu = time.process_time() - t0
    cov.stop()
    br_classes.stop()
    data = cov.get_data()
    lines = {}
    for f in data.measured_files():
        ls = data.lines(f)
        if ls:
            lines[f[len(ROOT) + 1 :]] = sorted(ls)
    rec = {
        "n": test["number"],
        "rc": int(rc),
        "cpu": round(cpu, 3),
        "lines": lines,
        "opened": sorted(opened),
        "classes": br_classes.snapshot(),
    }
    os.write(out_fd, (json.dumps(rec) + "\n").encode())


def main() -> None:
    tests = json.load(open(sys.argv[1]))["tests"]
    out_path = sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
    only = os.environ.get("ONLY")
    if only:
        keep = {int(x) for x in only.split(",")}
        tests = [t for t in tests if t["number"] in keep]

    os.chdir(ROOT)
    os.makedirs(os.environ["BR_TRACE_DIR"], exist_ok=True)
    preimport()
    import coverage  # noqa: F401  (import before fork)
    import pytest  # noqa: F401

    out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o644)
    devnull = os.open(os.devnull, os.O_WRONLY)
    running: dict[int, dict] = {}
    queue = sorted(tests, key=lambda t: -t["seconds"])  # long ones first
    done = 0
    start = time.time()
    while queue or running:
        while queue and len(running) < jobs:
            t = queue.pop(0)
            pid = os.fork()
            if pid == 0:
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                try:
                    child(t, out_fd)
                finally:
                    os._exit(0)
            running[pid] = t
        pid, status = os.wait()
        if pid not in running:  # some stray grandchild
            continue
        t = running.pop(pid)
        done += 1
        if os.waitstatus_to_exitcode(status) != 0:
            print(f"child for {t['id']} exited {status}", file=sys.stderr)
        if done % 100 == 0:
            print(f"{done}/{len(tests)} {time.time() - start:.0f}s", file=sys.stderr)
    print(f"done {done} in {time.time() - start:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
