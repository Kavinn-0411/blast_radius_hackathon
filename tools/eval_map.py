"""Offline: candidate recall/cost of the map alone on the sample set."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
from candidates import Map, parse_patch  # noqa: E402

m = Map(json.load(open(ROOT / "app/map.json")))
tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
answers = json.load(open(ROOT / "sample/answers.json"))["commits"]
sec = {t["number"]: t["seconds"] for t in tests}
GIANT = {t["number"] for t in tests if t["seconds"] >= 1.5}

tot_miss = 0
for cid in sorted(answers):
    a = answers[cid]
    cand: set[int] = set()
    notes = []
    for fd in parse_patch((ROOT / f"sample/commits/{cid}.patch").read_text()):
        path = fd.old or fd.new
        if path in m.files:
            lines = set(fd.removed) | {x for i in fd.inserted_at for x in (i - 1, i)}
            ts, module, _ = m.tests_for_lines(path, lines)
            cand |= ts
            if module:
                notes.append("module:" + path.split("/")[-1])
        else:
            cand |= set(m.opened.get(path, []))
    fail = set(a["failing"])
    miss = fail - cand
    tot_miss += bool(miss)
    print(
        f"{cid} {a['kind']:8s} fail={len(fail):3d} cand={len(cand):4d} "
        f"cand_s={sum(sec[t] for t in cand):6.1f} giants={sorted(cand & GIANT)} "
        f"miss={sorted(miss)} {' '.join(notes)}"
    )
print("missed commits:", tot_miss)
