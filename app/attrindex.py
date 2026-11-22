"""Which traced function units read a given attribute name (pinned source only).

Used to narrow class-constant edits: changing `JOIN_HINTS = False` in a dialect
generator can only matter to tests that ran code reading `JOIN_HINTS`. Names
that are read outside function bodies (class bodies, module level, default
arguments), or that appear anywhere in the test sources, are marked unsafe and
keep the broad class-user selection.

Build step: python attrindex.py MAP_JSON SRC_ROOT OUT_PKL
"""

import ast
import bisect
import os
import pickle
import re
import sys

UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _innermost(units, starts, line):
    i = bisect.bisect_right(starts, line) - 1
    while i >= 0:
        s, _, e, _ = units[i]
        if s <= line <= e:
            return i
        i -= 1
    return -1


def build(data: dict, src: str) -> tuple[dict, list]:
    reads: dict = {}
    unsafe: set = set()
    for path, d in data["files"].items():
        if not path.endswith(".py"):
            continue
        try:
            with open(os.path.join(src, path), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except Exception:
            continue
        units = d["units"]
        starts = [u[0] for u in units]
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            else:
                continue
            if not UPPER.match(name):
                continue
            if isinstance(node, (ast.Attribute, ast.Name)) and not isinstance(node.ctx, ast.Load):
                continue  # an assignment, not a read
            line = node.lineno
            u = _innermost(units, starts, line)
            if u < 0 or line < units[u][1]:
                unsafe.add(name)  # read at import/class-creation time
                continue
            reads.setdefault(name, set()).add((path, u))
    # anything the tests themselves mention may be read there directly
    for root, _, names in os.walk(os.path.join(src, "tests")):
        for n in names:
            if n.endswith(".py"):
                try:
                    with open(os.path.join(root, n), encoding="utf-8", errors="replace") as fh:
                        unsafe.update(re.findall(r"\b[A-Z][A-Z0-9_]*\b", fh.read()))
                except Exception:
                    pass
    return {k: sorted(v) for k, v in reads.items() if k not in unsafe}, sorted(unsafe)


def main() -> None:
    import json

    map_json, src, out = sys.argv[1:4]
    with open(map_json) as fh:
        data = json.load(fh)
    data["attr_reads"], data["attr_unsafe"] = build(data, src)
    with open(out, "wb") as fh:
        pickle.dump(data, fh, protocol=5)
    print(f"attr index: {len(data['attr_reads'])} names, {len(data['attr_unsafe'])} unsafe")


if __name__ == "__main__":
    main()
