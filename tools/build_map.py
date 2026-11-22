"""Offline: turn the per-test trace into the compact map the selector loads.

Inputs : work/out/trace.jsonl, work/out/sub/*.json (spawned-worker coverage),
         work/sqlglot (pinned checkout), sample/tests.json
Output : app/map.json

Map layout (test numbers are ints; sets stored as sorted lists):
  files[path] = {
      "units":  [[start, body_start, end, parent_idx], ...]   # functions/lambdas (AST)
      "unit_tests": [[tests...], ...]                          # ran any body line
      "mod_tests": [tests...]                                  # ran any line in file
  }
  family[path] = [paths...]          # dialect family (dialects/x, parsers/x, ...)
  opened[path] = [tests...]          # non-sqlglot files read during a test
"""

import ast
import json
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "work/sqlglot"
OUT = ROOT / "app/map.json"


def load_trace():
    lines = defaultdict(lambda: defaultdict(set))  # file -> line -> tests
    opened = defaultdict(set)
    classes = defaultdict(set)  # "module:qualname" -> tests that instantiated it (MRO)
    for l in open(ROOT / "work/out" / os.environ.get("TRACE", "trace.jsonl")):
        r = json.loads(l)
        for f, ls in r["lines"].items():
            for x in ls:
                lines[f][x].add(r["n"])
        for f in r["opened"]:
            opened[f].add(r["n"])
        for c in r.get("classes", []):
            classes[c].add(r["n"])
    sub = ROOT / "work/out" / os.environ.get("SUB", "sub")
    for p in sub.iterdir():
        n = int(p.name.split("-")[0])
        for f, ls in json.load(open(p)).items():
            if f == "__classes__":
                for c in ls:
                    classes[c].add(n)
                continue
            for x in ls:
                lines[f][x].add(n)
    return lines, opened, classes


def classes_of(path: Path, module: str):
    """Classes (not inside functions) and the statements directly in their bodies.
    Returns (classes=[[start, end, key]], cstmts=[[start, end, class_idx, [names]]],
    body_attr_refs={class_idx: set(attr names read in its body)})."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes, cstmts, refs = [], [], {}

    def assigned(stmt):
        names = set()
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        return names

    def body_refs(stmt, acc):
        # attribute names read by class-body code (skip nested defs/classes)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if not isinstance(stmt, ast.Lambda):
                for d in stmt.decorator_list:
                    body_refs(d, acc)
            return
        for n in ast.iter_child_nodes(stmt):
            if isinstance(n, ast.Attribute):
                chain, v = [], n.value
                while isinstance(v, ast.Attribute):
                    chain.append(v.attr)
                    v = v.value
                if isinstance(v, ast.Name):
                    chain.append(v.id)
                acc.add((n.attr, ".".join(reversed(chain))))
            body_refs(n, acc)

    def visit(node, prefix):
        for stmt in getattr(node, "body", []):
            if isinstance(stmt, ast.ClassDef):
                qual = f"{prefix}.{stmt.name}" if prefix else stmt.name
                start = min([stmt.lineno] + [d.lineno for d in stmt.decorator_list])
                classes.append([start, stmt.end_lineno, f"{module}:{qual}"])
                ci = len(classes) - 1
                acc = set()
                for s in stmt.body:
                    s0 = min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", [])])
                    cstmts.append([s0, s.end_lineno, ci, sorted(assigned(s))])
                    body_refs(s, acc)
                refs[ci] = acc
                visit(stmt, qual)
            elif isinstance(stmt, (ast.If, ast.Try, ast.With)):
                visit(stmt, prefix)  # classes defined under if TYPE_CHECKING etc.

    visit(tree, "")
    cstmts.sort()
    return classes, cstmts, refs


def load_costs():
    """Hierarchical CPU costs under our harness, from single-test measurements.
    One measurement = session start + file (collect/import) + class setup + test.
    session  = cheapest measurement overall
    file     = cheapest in the file  - session
    class    = cheapest in the class - cheapest in the file
    test     = measurement           - cheapest in its class
    Returns (test_cost, file_cost, class_cost, session_cost)."""
    p = ROOT / "work/out/costs.json"
    if not p.exists():
        return {}, {}, {}, 0.0
    tests = {t["number"]: t for t in json.load(open(ROOT / "sample/tests.json"))["tests"]}
    raw = {int(n): r for n, r in json.load(open(p)).items()}
    ok = {n: r["cpu"] for n, r in raw.items() if r["rc"] == 0}
    cls_of = {n: "::".join(tests[n]["id"].split("::")[:-1]) for n in tests}
    per_file, per_cls = defaultdict(list), defaultdict(list)
    for n, c in ok.items():
        per_file[tests[n]["file"]].append(c)
        per_cls[cls_of[n]].append(c)
    session = min(ok.values())
    fmin = {f: min(v) for f, v in per_file.items()}
    cmin = {k: min(v) for k, v in per_cls.items()}
    file_cost = {f: round(v - session, 3) for f, v in fmin.items()}
    class_cost = {k: round(v - fmin[k.split("::")[0]], 3) for k, v in cmin.items()}
    cost = {}
    for n, r in raw.items():
        cost[n] = round(ok[n] - cmin[cls_of[n]], 3) if n in ok else 99.0
    return cost, file_cost, class_cost, round(session, 3)


def module_name(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def units_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    units = []

    def visit(node, parent):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                start = min([child.lineno] + [d.lineno for d in getattr(child, "decorator_list", [])])
                if isinstance(child, ast.Lambda):
                    body_start = child.body.lineno
                else:
                    body_start = child.body[0].lineno
                units.append([start, body_start, child.end_lineno, parent])
                visit(child, len(units) - 1)
            else:
                visit(child, parent)

    visit(tree, -1)
    return units


COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def stmts_of(path: Path):
    """Every statement as [start, header_end, end]; start includes decorators.
    header_end: last line of a compound statement's header (== end for simple)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            if isinstance(node, COMPOUND):
                first = node.body[0] if getattr(node, "body", None) else None
                if isinstance(node, ast.Match):
                    first = node.cases[0].pattern if node.cases else None
                hdr = (first.lineno - 1) if first is not None else node.end_lineno
                hdr = max(hdr, start)
            else:
                hdr = node.end_lineno
            out.append([start, hdr, node.end_lineno])
    out.sort()
    return out


def test_ranges(src: Path, tests: list):
    """test number -> [start, end] of its def in its file (for edits to test files)."""
    out = {}
    by_file = defaultdict(list)
    for t in tests:
        by_file[t["file"]].append(t)
    for f, ts in by_file.items():
        tree = ast.parse((src / f).read_text(encoding="utf-8"))
        defs = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        start = min([item.lineno] + [d.lineno for d in item.decorator_list])
                        defs[(node.name, item.name)] = [start, item.end_lineno]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault((None, node.name), [node.lineno, node.end_lineno])
        for t in ts:
            parts = t["id"].split("::")
            key = (parts[1], parts[2]) if len(parts) == 3 else (None, parts[-1])
            if key in defs:
                out[t["number"]] = defs[key]
    return out


FAMILY_DIRS = ["dialects", "parsers", "generators", "tokenizers", "typing"]


def main():
    lines, opened, class_users = load_trace()
    enums = set(json.load(open(ROOT / "work/out/enums.json")))
    files = {}
    attr_xref = defaultdict(set)  # attr -> {(class key whose body reads X.attr, "X")}
    for path in sorted(SRC.glob("sqlglot/**/*.py")):
        rel = path.relative_to(SRC).as_posix()
        units = units_of(path)
        cov = lines.get(rel, {})
        unit_tests = []
        for start, body_start, end, _ in units:
            s = set()
            for x in range(body_start, end + 1):
                s |= cov.get(x, set())
            unit_tests.append(sorted(s))
        mod = set()
        for ts in cov.values():
            mod |= ts
        classes, cstmts, refs = classes_of(path, module_name(rel))
        class_tests = []
        for ci, (_, _, key) in enumerate(classes):
            # enums are used via attribute access, never "instantiated": no users
            users = [] if key in enums else sorted(class_users.get(key, ()))
            class_tests.append(users)
            for a, chain in refs.get(ci, ()):
                attr_xref[a].add((key, chain))
        files[rel] = {
            "units": units,
            "unit_tests": unit_tests,
            "mod_tests": sorted(mod),
            "stmts": stmts_of(path),
            "classes": classes,
            "class_tests": class_tests,
            "cstmts": cstmts,
        }

    tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
    test_files = {}
    for path in sorted(SRC.glob("tests/**/*.py")):
        rel = path.relative_to(SRC).as_posix()
        try:
            test_files[rel] = {"stmts": stmts_of(path)}
        except SyntaxError:
            pass
    ranges = test_ranges(SRC, tests)

    # dialect families: sqlglot/<dir>/<name>.py grouped by name
    by_name = defaultdict(list)
    for rel in files:
        m = re.match(r"sqlglot/(\w+)/(\w+)\.py$", rel)
        if m and m.group(1) in FAMILY_DIRS and m.group(2) != "__init__":
            by_name[m.group(2)].append(rel)
    family = {rel: sorted(v) for v in by_name.values() for rel in v}

    costs = load_costs()
    OUT.parent.mkdir(exist_ok=True)
    data = {
        "files": files,
        "family": family,
        "opened": {k: sorted(v) for k, v in opened.items()},
        "test_files": test_files,
        "test_ranges": ranges,
        "core": json.load(open(ROOT / "work/out/core_modules.json")),
        "attr_xref": {k: sorted(v) for k, v in attr_xref.items()},
        # CPU each test costs under the selector's own harness (inline pools);
        # tests that timed out/failed there are marked expensive
        "cost": costs[0],
        "file_cost": costs[1],
        "class_cost": costs[2],
        "session_cost": costs[3],
    }
    with open(OUT, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"wrote {OUT} {os.path.getsize(OUT) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
