"""Generate sample-style mutant patches against the pinned sqlglot (offline, lab).

Usage: python mutants.py /sqlglot OUT_DIR N SEED
Writes OUT_DIR/commits/<id>.patch and OUT_DIR/kinds.json. Labels come from
label.py. These are our own evaluation commits, never shipped in the image.
"""

import difflib
import io
import json
import os
import random
import re
import sys
import tokenize

SRC, OUT, N, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(SEED)

SWAP = {
    "and": "or", "or": "and", "==": "!=", "!=": "==", "<": "<=", "<=": "<",
    ">": ">=", ">=": ">", "is": "==", "True": "False", "False": "True",
    "+": "-", "-": "+", "in": "not in",
}
COMMENTS = ["# Cheap check first", "# Keep in sync with the generator",
            "# Kept for backwards compatibility", "# Intentionally not cached",
            "# Mirrors the behavior of the reference implementation", "# TODO: revisit"]


def py_files():
    out = []
    for root, _, files in os.walk(os.path.join(SRC, "sqlglot")):
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
    return sorted(out)


def fixture_files():
    out = []
    for root, _, files in os.walk(os.path.join(SRC, "tests", "fixtures")):
        for f in files:
            if f.endswith(".sql"):
                out.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
    return sorted(out)


def read(path):
    with open(os.path.join(SRC, path), encoding="utf-8") as fh:
        return fh.read().split("\n")


def mutate_code(path, lines):
    text = "\n".join(lines)
    toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)]
    cands = []
    for t in toks:
        if t.start[0] != t.end[0]:
            continue
        if (t.type == tokenize.OP or t.type == tokenize.NAME) and t.string in SWAP:
            cands.append((t, SWAP[t.string]))
        elif t.type == tokenize.NUMBER and re.fullmatch(r"\d+", t.string):
            cands.append((t, str(int(t.string) + rng.choice([1, -1]) if t.string != "0" else "1")))
        elif t.type == tokenize.NAME and t.string == "not":
            cands.append((t, ""))
    if not cands:
        return None
    t, rep = rng.choice(cands)
    ln, c0 = t.start
    _, c1 = t.end
    line = lines[ln - 1]
    new = line[:c0] + rep + line[c1:]
    if rep == "":
        new = line[:c0] + line[c1:].lstrip(" ")
    if new == line:
        return None
    out = list(lines)
    out[ln - 1] = new
    return out


def mutate_comment(path, lines):
    # insert an own-line comment at the indentation of the next code line
    for _ in range(50):
        i = rng.randrange(1, len(lines))
        nxt = lines[i]
        if nxt.strip() and not nxt.strip().startswith(("#", ")", "]", "}")):
            indent = nxt[: len(nxt) - len(nxt.lstrip())]
            return lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:]
    return None


def mutate_fixture(path, lines):
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and " " in l]
    if not idx:
        return None
    i = rng.choice(idx)
    l = lines[i]
    spaces = [m.start() for m in re.finditer(" ", l)]
    p = rng.choice(spaces)
    tok = rng.choice(["42", "x", "0", "NULL", "1", "y42"])
    out = list(lines)
    out[i] = l[:p] + " " + tok + l[p:]
    return out


def diff(path, old, new):
    body = list(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm="", n=3))
    return f"diff --git a/{path} b/{path}\nindex 1a2b3c4..5d6e7f8 100644\n" + "\n".join(body) + "\n"


def one(kind, pyf, fxf):
    for _ in range(100):
        if kind == "fixture":
            path = rng.choice(fxf)
            new = mutate_fixture(path, read(path))
        else:
            path = rng.choice(pyf)
            fn = mutate_code if kind == "code" else mutate_comment
            new = fn(path, read(path))
        if new:
            return path, diff(path, read(path), new)
    raise RuntimeError(kind)


def main():
    pyf, fxf = py_files(), fixture_files()
    os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
    kinds = {}
    for k in range(N):
        kind = rng.choices(["code", "comment", "fixture", "multi"], [55, 10, 20, 15])[0]
        cid = f"m{SEED}_{k:03d}"
        if kind == "multi":
            parts, used = [], set()
            for sub in rng.sample(["code", "comment", "fixture"], 2) + [rng.choice(["code", "comment"])]:
                path, d = one(sub, pyf, fxf)
                if path not in used:
                    used.add(path)
                    parts.append(d)
            patch = "".join(parts)
        else:
            _, patch = one(kind, pyf, fxf)
        with open(os.path.join(OUT, "commits", cid + ".patch"), "w", newline="\n") as fh:
            fh.write(patch)
        kinds[cid] = kind
    json.dump(kinds, open(os.path.join(OUT, "kinds.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
