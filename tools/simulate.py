"""Offline policy simulator: real analysis + plan, perfect in-container runs.

Usage: python tools/simulate.py SET [SET ...]   (dirs with commits/ + answers.json)
Env overrides: GATE=0/1, MODE=evidence|any, GIANT=1.0
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("BR_SRC", str(ROOT / "work/sqlglot"))

import policy  # noqa: E402
from analyze import Analyzer  # noqa: E402
from candidates import Map  # noqa: E402
from pack import pack  # noqa: E402

if "GATE" in os.environ:
    policy.GATE_GIANTS = os.environ["GATE"] == "1"
if "MODE" in os.environ:
    policy.GATE_MODE = os.environ["MODE"]
if "GIANT" in os.environ:
    policy.GIANT_S = float(os.environ["GIANT"])

tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
sec = {t["number"]: t["seconds"] for t in tests}
known = set(ms)
_d = json.load(open(ROOT / "app/map.json"))
if os.environ.get("ATTR", "1") == "1":
    import attrindex  # noqa: E402
    _d["attr_reads"], _d["attr_unsafe"] = attrindex.build(_d, str(ROOT / "work/sqlglot"))
m = Map(_d)
for t in tests:
    m.test_file_of[t["number"]] = (t["file"], t["id"])
an = Analyzer(m, sorted(known))
_mapdata = json.load(open(ROOT / "app/map.json"))
policy.COST.update({int(k): v for k, v in _mapdata.get("cost", {}).items()})
policy.FILE_COST.update(_mapdata.get("file_cost", {}))
policy.CLASS_COST.update(_mapdata.get("class_cost", {}))
if _mapdata.get("session_cost"):
    policy.SESSION[0] = _mapdata["session_cost"]
verbose = os.environ.get("V") == "1"
policy.SPEED[0] = float(os.environ.get("SPEED", "1.3"))

for d in sys.argv[1:]:
    ans = json.load(open(ROOT / d / "answers.json"))["commits"]
    tot = cpu = orc = 0.0
    misses = []
    for cid in sorted(ans):
        fail = set(ans[cid]["failing"])
        res = an.analyze((ROOT / d / "commits" / f"{cid}.patch").read_text(encoding="utf-8"))
        kinds = {n.split(":", 1)[0] for n in res.notes}
        p = policy.make_plan(res, sec, ms, known, m.test_file_of, 1e9,
                             bool(kinds) and kinds <= {"data", "noop"})
        c = p.est_s if p.check else 0.0
        chosen = policy.finish(p, p.check & fail)
        cost = pack(sorted(chosen), ms)[1] / 1000
        miss = fail - chosen
        o = pack(sorted(fail), ms)[1] / 1000
        tot += cost + 300 * bool(miss)
        cpu += c
        orc += o
        if miss:
            misses.append((cid, ans[cid]["kind"], sorted(miss), " ".join(res.notes)[:150]))
        if verbose or miss:
            print(f"  {cid} {ans[cid]['kind']:8s} cost={cost:6.1f} cpu~{c:4.1f} oracle={o:6.1f} "
                  f"blind={len(p.blind)} check={len(p.check)} gated={len(p.gated)} "
                  f"fail={len(fail)}{' MISS ' + str(sorted(miss)) if miss else ''}")
    n = len(ans)
    print(f"{d}: n={n} simulated={tot:.1f} cpu~{cpu:.1f} total~{tot + cpu:.1f} "
          f"oracle={orc:.1f} missed_commits={len(misses)}  per-commit~{(tot + cpu) / n:.2f}")
    for x in misses:
        print("   MISS", x)
