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
    for l in open(ROOT / "work/out/trace.jsonl"):
        r = json.loads(l)
        for f, ls in r["lines"].items():
            for x in ls:
                lines[f][x].add(r["n"])
        for f in r["opened"]:
            opened[f].add(r["n"])
    sub = ROOT / "work/out/sub"
    for p in sub.iterdir():
        n = int(p.name.split("-")[0])
        for f, ls in json.load(open(p)).items():
            for x in ls:
                lines[f][x].add(n)
    return lines, opened


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
    lines, opened = load_trace()
    files = {}
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
        files[rel] = {
            "units": units,
            "unit_tests": unit_tests,
            "mod_tests": sorted(mod),
            "stmts": stmts_of(path),
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

    OUT.parent.mkdir(exist_ok=True)
    data = {
        "files": files,
        "family": family,
        "opened": {k: sorted(v) for k, v in opened.items()},
        "test_files": test_files,
        "test_ranges": ranges,
        "core": json.load(open(ROOT / "work/out/core_modules.json")),
    }
    with open(OUT, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"wrote {OUT} {os.path.getsize(OUT) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
