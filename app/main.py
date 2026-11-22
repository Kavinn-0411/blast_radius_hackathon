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
import verify  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
MAP = os.environ.get("BR_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.pkl"))

# policy knobs
GIANT_S = 1.0  # never run tests at least this long ourselves; select them blind
VERIFY_OVERHEAD_S = 0.8  # fork + import patched sqlglot + collect
VERIFY_FACTOR = 1.3  # our CPU per recorded test second
PARALLEL = 2
DEADLINE_S = float(os.environ.get("BR_DEADLINE", "150"))


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
    verify.preload()

    plans = []
    for cid in commits:
        try:
            with open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="replace") as fh:
                res = an.analyze(fh.read())
        except Exception as e:
            log(f"{cid}: analysis failed {e!r}; selecting everything")
            emit(cid, known)
            continue
        cand = res.tests & known
        blind = {t for t in cand if sec[t] >= GIANT_S}
        check = cand - blind
        if check:
            _, cost_blind = pack(sorted(cand), ms)
            _, cost_giants = pack(sorted(blind), ms)
            est = (VERIFY_OVERHEAD_S + VERIFY_FACTOR * sum(sec[t] for t in check)) * 1000
            if est + cost_giants >= cost_blind:
                blind, check = cand, set()
        log(f"{cid}: cand={len(cand)} blind={len(blind)} check={len(check)} {' '.join(res.notes)}")
        plans.append((cid, res, blind, check))

    queue = []
    for cid, res, blind, check in plans:
        if check:
            queue.append((cid, res, blind, check))
        else:
            emit(cid, blind)

    running: list = []
    while queue or running:
        while queue and len(running) < PARALLEL:
            cid, res, blind, check = queue.pop(0)
            if time.monotonic() - t0 > DEADLINE_S:
                emit(cid, blind | check)  # out of time: select blind
                continue
            ids = [m.test_file_of[t][1] for t in sorted(check)]
            left = DEADLINE_S + 15 - (time.monotonic() - t0)
            budget = max(3.0, min(20 + 4 * sum(sec[t] for t in check), left))
            job = verify.Job(cid, res.files, ids, budget)
            job.meta = (cid, blind, check)
            running.append(job)
        if not running:
            break
        time.sleep(0.01)
        for job in list(running):
            if job.poll():
                running.remove(job)
                cid, blind, check = job.meta
                failed = {by_id[i] for i in job.result if i in by_id}
                log(f"{cid}: verified {len(check)} -> {len(failed)} failing "
                    f"({time.monotonic() - job.start:.1f}s)")
                emit(cid, blind | failed)
    log(f"done {len(commits)} commits in {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    main()
