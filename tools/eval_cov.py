"""Offline: how well does raw per-test line coverage predict the sample failures?"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
trace = [json.loads(l) for l in open(ROOT / "work/out/trace.jsonl")]
tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
answers = json.load(open(ROOT / "sample/answers.json"))["commits"]
sec = {t["number"]: t["seconds"] for t in tests}

cov = defaultdict(set)  # (file, line) -> tests
opened = defaultdict(set)
for r in trace:
    for f, ls in r["lines"].items():
        for l in ls:
            cov[(f, l)].add(r["n"])
    for f in r["opened"]:
        opened[f].add(r["n"])
    if r["rc"] != 0:
        print("trace: test failed at base", r["n"], file=sys.stderr)


def changed_lines(patch: str):
    """old-file line numbers touched, per file; pure insertions -> neighbours."""
    out = defaultdict(set)
    f = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            f = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)", line)
            old = int(m.group(1))
        elif f is None or line.startswith("---") or line.startswith("diff ") or line.startswith("index "):
            continue
        elif line.startswith("-"):
            out[f].add(old)
            old += 1
        elif line.startswith("+"):
            out[f].update({old - 1, old})
        else:
            old += 1
    return out


for cid in sorted(answers):
    a = answers[cid]
    ch = changed_lines((ROOT / f"sample/commits/{cid}.patch").read_text())
    cand = set()
    for f, ls in ch.items():
        if f.startswith("sqlglot/"):
            for l in ls:
                cand |= cov.get((f, l), set())
        else:
            cand |= opened.get(f, set())
    fail = set(a["failing"])
    miss = fail - cand
    print(
        f"{cid} {a['kind']:8s} fail={len(fail):3d} cand={len(cand):4d} "
        f"cand_s={sum(sec[t] for t in cand):6.1f} miss={sorted(miss)}"
    )
