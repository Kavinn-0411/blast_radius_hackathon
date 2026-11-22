"""Per-commit cost of output.jsonl vs the oracle (exactly the failing tests)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
from pack import pack  # noqa: E402

tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
SET = sys.argv[1] if len(sys.argv) > 1 else "sample"
ans = json.load(open(ROOT / SET / "answers.json"))["commits"]
tot = orc = 0
for line in open(ROOT / "output.jsonl"):
    r = json.loads(line)
    ws = [w for w in r["workers"] if w]
    cost = (max(sum(ms[t] for t in w) for w in ws) + 2000 * len(ws)) if ws else 0
    sel = {t for w in ws for t in w}
    fail = set(ans[r["commit"]]["failing"])
    miss = bool(fail - sel)
    o = pack(sorted(fail), ms)[1]
    tot += cost + 300000 * miss
    orc += o
    print(f"{r['commit']} {ans[r['commit']]['kind']:8s} cost={cost/1000:6.1f} oracle={o/1000:6.1f} "
          f"sel={len(sel):4d} fail={len(fail):3d}{' MISS ' + str(sorted(fail - sel)) if miss else ''}")
print(f"total={tot/1000:.1f} oracle={orc/1000:.1f}")
