"""Label mutant commits by running the FULL suite on each (offline, lab).

Usage: python label.py OUT_DIR SLOTS XDIST_WORKERS
Reads OUT_DIR/commits/*.patch, writes OUT_DIR/answers.json in the sample format
({"commits": {id: {"failing": [...], "kind": k}}}). Resumable.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

OUT, SLOTS, XW = sys.argv[1], int(sys.argv[2]), sys.argv[3]
TESTS = json.load(open("/data/tests.json"))["tests"]
BY_ID = {t["id"]: t["number"] for t in TESTS}
BY_FILE: dict = {}
for t in TESTS:
    BY_FILE.setdefault(t["file"], []).append(t["number"])

PLUGIN = '''
import json, os
FAILED = set(); BADFILES = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        BADFILES.add(report.nodeid.split("::")[0])
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    with open(os.environ["BR_LABEL_OUT"], "w") as fh:
        json.dump({"failed": sorted(FAILED), "bad": sorted(BADFILES), "rc": int(exitstatus)}, fh)
'''


def label(cid: str, slot: int) -> dict:
    work = f"/work/slot{slot}"
    if not os.path.exists(work):
        shutil.copytree("/sqlglot", work, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        with open(f"{work}/br_label_plugin.py", "w") as fh:
            fh.write(PLUGIN)
    patch = os.path.join(OUT, "commits", cid + ".patch")
    subprocess.run(["patch", "-p1", "-s", "-i", patch], cwd=work, check=True)
    out = f"/tmp/label_{slot}.json"
    try:
        env = dict(os.environ, PYTHONPATH=work, BR_LABEL_OUT=out, SKIP_INTEGRATION="0")
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "br_label_plugin",
             "-n", XW, "--timeout", "300", "tests"],
            cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900,
        )
        r = json.load(open(out))
        failing = {BY_ID[n] for n in r["failed"] if n in BY_ID}
        for f in r["bad"]:
            failing.update(BY_FILE.get(f, []))
        # nodeids with a [param] or subtest suffix
        for n in r["failed"]:
            base = n.split("[")[0]
            if base in BY_ID:
                failing.add(BY_ID[base])
        return {"failing": sorted(failing), "rc": r["rc"]}
    finally:
        subprocess.run(["patch", "-p1", "-s", "-R", "-i", patch], cwd=work, check=True)


def main():
    kinds = json.load(open(os.path.join(OUT, "kinds.json")))
    ans_path = os.path.join(OUT, "answers.json")
    answers = json.load(open(ans_path))["commits"] if os.path.exists(ans_path) else {}
    todo = [c for c in sorted(kinds) if c not in answers]
    free = list(range(SLOTS))
    t0 = time.time()

    def run(cid):
        slot = free.pop()
        try:
            r = label(cid, slot)
        except Exception as e:
            print(f"{cid} label error {e!r}", file=sys.stderr)
            r = None
        finally:
            free.append(slot)
        return cid, r

    with ThreadPoolExecutor(SLOTS) as ex:
        for i, (cid, r) in enumerate(ex.map(run, todo), 1):
            if r is None:
                continue
            answers[cid] = {"failing": r["failing"], "kind": kinds[cid]}
            json.dump({"commits": answers}, open(ans_path + ".tmp", "w"))
            os.replace(ans_path + ".tmp", ans_path)
            print(f"[{i}/{len(todo)} {time.time() - t0:.0f}s] {cid} {kinds[cid]} "
                  f"fail={len(r['failing'])} rc={r['rc']}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
