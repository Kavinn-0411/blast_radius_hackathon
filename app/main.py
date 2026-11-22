"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr."""

# pytest must be imported before sqlglot anywhere sqlglot is imported: spawned
# pool workers re-import this module first, and sqlglot reads
# `"pytest" in sys.modules` at import time (UNITTEST flag).
import pytest  # noqa: F401

import json
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyze import Analyzer, log  # noqa: E402
from candidates import Map  # noqa: E402
from pack import pack  # noqa: E402
import policy  # noqa: E402
import verify  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
MAP = os.environ.get("BR_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.pkl"))

PARALLEL = 2
CPU_BUDGET_PER_COMMIT = 2.5  # seconds of test-running CPU per commit, batch-wide
DEADLINE_S = float(os.environ.get("BR_DEADLINE", "150"))
SPEED = 1.3  # assumed CPU seconds here per CPU second where costs were measured


EMITTED: set = set()
ALL_MS: dict = {}


def emit(cid, chosen):
    if cid in EMITTED:
        return
    workers, _ = pack(sorted(chosen), ALL_MS)
    sys.stdout.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
    sys.stdout.flush()
    EMITTED.add(cid)


def main() -> None:
    commits = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    with open(os.path.join(DATA, "tests.json")) as fh:
        tests = json.load(fh)["tests"]
    ALL_MS.update({t["number"]: round(t["seconds"] * 1000) for t in tests})
    try:
        run(commits, tests)
    except BaseException as e:  # never lose the run: whatever is left runs everything
        log(f"FATAL {e!r}")
        import traceback
        traceback.print_exc()
    finally:
        everything = set(ALL_MS)
        for cid in commits:
            if cid not in EMITTED:
                log(f"{cid}: fallback to all tests")
                emit(cid, everything)


def run(commits, tests) -> None:
    t0 = time.monotonic()
    ms = ALL_MS
    sec = {t["number"]: t["seconds"] for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    if MAP.endswith(".json"):
        data = json.load(open(MAP))
    else:
        with open(MAP, "rb") as fh:
            data = pickle.load(fh)
    m = Map(data)
    known = set(ms)
    for t in tests:
        m.test_file_of[t["number"]] = (t["file"], t["id"])
    an = Analyzer(m, sorted(known))

    os.makedirs(verify.TMP, exist_ok=True)
    # the board's machine is likely slower than the one costs were measured on;
    # a startup benchmark proved too noisy to tell (1.65x on the reference box)
    speed = policy.SPEED[0] = SPEED
    verify.preload()

    plans = []
    policy.COST.update({int(k): v for k, v in data.get("cost", {}).items()})
    policy.FILE_COST.update(data.get("file_cost", {}))
    policy.CLASS_COST.update(data.get("class_cost", {}))
    if data.get("session_cost"):
        policy.SESSION[0] = data["session_cost"]
    # hard cap on CPU we spend running tests across the whole batch
    budget_left = CPU_BUDGET_PER_COMMIT * len(commits)
    for cid in commits:
        try:
            with open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="replace") as fh:
                res = an.analyze(fh.read())
        except Exception as e:
            log(f"{cid}: analysis failed {e!r}; selecting everything")
            emit(cid, known)
            continue
        kinds = {n.split(":", 1)[0] for n in res.notes}
        data_only = bool(kinds) and kinds <= {"data", "noop"}
        p = policy.make_plan(res, sec, ms, known, m.test_file_of, budget_left, data_only)
        budget_left -= p.est_s
        log(f"{cid}: blind={len(p.blind)} check={len(p.check)} gated={len(p.gated)} "
            f"{' '.join(res.notes)}")
        plans.append((cid, res, p))

    queue = []
    for cid, res, p in plans:
        if p.check:
            queue.append((cid, res, p))
        else:
            emit(cid, p.blind)

    running: list = []
    while queue or running:
        while queue and len(running) < PARALLEL:
            cid, res, p = queue.pop(0)
            check = p.check
            if time.monotonic() - t0 > DEADLINE_S:
                emit(cid, p.blind | p.check | set(p.gated))  # out of time: select blind
                continue
            ids = [m.test_file_of[t][1] for t in sorted(check)]
            left = DEADLINE_S + 15 - (time.monotonic() - t0)
            # CPU cap: stop between tests once a run costs well over its estimate
            # (failing tests can run far slower than passing ones: 3utz 3.8x);
            # unfinished tests then count as failing
            cap = 2.0 * p.est_s * speed + 0.8
            # wall backstop for a single runaway test
            budget = max(3.0, min((10 + 6 * p.est_s) * speed, left))
            job = verify.Job(cid, res.files, ids, budget, cap)
            job.meta = (cid, p)
            running.append(job)
        if not running:
            break
        time.sleep(0.01)
        for job in list(running):
            if job.poll():
                running.remove(job)
                cid, p = job.meta
                failed = {by_id[i] for i in job.result if i in by_id}
                chosen = policy.finish(p, failed)
                log(f"{cid}: verified {len(p.check)} -> {len(failed)} failing, "
                    f"giants kept {len(chosen & set(p.gated))}/{len(p.gated)} "
                    f"({time.monotonic() - job.start:.1f}s wall, cpu {job.cpu:.2f} est {p.est_s:.2f})")
                emit(cid, chosen)
    log(f"done {len(commits)} commits in {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    main()
