"""Per-commit analysis: apply the patch in memory, drop no-op edits, map the rest
to candidate tests. Only the edited statements are ever parsed."""

import ast
import bisect
import os
import re
import sys
from dataclasses import dataclass, field

from candidates import FileDiff, Map, parse_patch

SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
NL = chr(10)
_COOKIE = re.compile(rb"^[ \t\f]*#.*?coding[:=]")


def log(*a):
    print(*a, file=sys.stderr)


@dataclass
class Block:
    """One contiguous run of -/+ lines. Old lines [lo, hi] removed (hi < lo: pure
    insertion before old line lo)."""
    lo: int
    hi: int
    minus: list[str]
    plus: list[str]


def blocks_of(fd: FileDiff) -> list[Block]:
    out = []
    for start, raw in fd.hunks:
        ln = start
        cur = None
        for line in raw:
            tag = line[:1]
            if tag in "-+":
                if cur is None:
                    cur = Block(ln, ln - 1, [], [])
                if tag == "-":
                    cur.minus.append(line[1:])
                    cur.hi = ln
                    ln += 1
                else:
                    cur.plus.append(line[1:])
            else:
                if cur is not None:
                    out.append(cur)
                    cur = None
                ln += 1
        if cur is not None:
            out.append(cur)
    return out


def apply_blocks(old_lines: list[str], blocks: list[Block]) -> list[str]:
    new = []
    pos = 1
    for b in blocks:
        new.extend(old_lines[pos - 1 : b.lo - 1])
        new.extend(b.plus)
        pos = b.hi + 1 if b.hi >= b.lo else b.lo
    new.extend(old_lines[pos - 1 :])
    return new


def _is_trivia(s: str) -> bool:
    t = s.strip()
    return t == "" or t.startswith("#")


def _parse_region(text: str):
    if text[:1] in " \t":
        text = "if 1:\n" + text
    return ast.dump(ast.parse(text))


@dataclass
class Result:
    tests: set = field(default_factory=set)
    files: dict = field(default_factory=dict)  # path -> new text (None = deleted)
    notes: list = field(default_factory=list)
    import_break: list = field(default_factory=list)


class Analyzer:
    def __init__(self, m: Map, all_tests: list[int]):
        self.m = m
        self.all_tests = all_tests
        self._lines_cache: dict[str, list[str]] = {}

    def old_lines(self, path: str) -> list[str]:
        if path not in self._lines_cache:
            with open(os.path.join(SRC, path), encoding="utf-8", newline="") as fh:
                self._lines_cache[path] = fh.read().split("\n")
        return self._lines_cache[path]

    # ---- python no-op detection -------------------------------------------------
    def _semantic_blocks(self, path: str, stmts: list, old: list[str], blocks: list[Block]):
        """Return (blocks that change behaviour, syntax_broken)."""
        semantic = []
        broken = False
        for b in blocks:
            if b.hi >= b.lo:  # lines lo..hi replaced
                lo, hi = b.lo, b.hi
                def holds(s, e, lo=lo, hi=hi):
                    return s <= lo and hi <= e
            else:  # insertion in the gap between old lines lo-1 and lo
                lo, hi = b.lo - 1, b.lo
                def holds(s, e, lo=lo, hi=hi):
                    return s <= lo and hi <= e
            trivia = all(_is_trivia(x) for x in b.minus + b.plus) and not (
                b.lo <= 3 and any(_COOKIE.match(x.encode()) for x in b.minus + b.plus)
            )
            if trivia and not any(holds(s, hdr) for s, hdr, _ in stmts):
                continue  # comment/blank lines between statements (or inside a body)
            # smallest statement containing the whole edit -> parse old vs new
            region = None
            for s, _, e in stmts:
                if holds(s, e) and (region is None or e - s < region[1] - region[0]):
                    region = (s, e)
            if region is None:
                region = (1, len(old))
            s, e = region
            old_text = NL.join(old[s - 1 : e])
            rel = Block(b.lo - s + 1, b.hi - s + 1, b.minus, b.plus)
            new_text = NL.join(apply_blocks(old[s - 1 : e], [rel]))
            try:
                same = _parse_region(old_text) == _parse_region(new_text)
            except SyntaxError:
                # the region may not parse alone (e.g. new `else:` clause): whole file
                try:
                    ast.parse(NL.join(apply_blocks(old, [b])))
                    same = False
                except SyntaxError:
                    broken = True
                    same = False
            if not same:
                semantic.append(b)
        return semantic, broken

    # ---- main -------------------------------------------------------------------
    def analyze(self, patch_text: str) -> Result:
        m = self.m
        res = Result()
        for fd in parse_patch(patch_text):
            path = fd.old or fd.new
            blocks = blocks_of(fd)
            exists = fd.old is not None and os.path.exists(os.path.join(SRC, fd.old))
            if exists:
                old = self.old_lines(fd.old)
                new_text = None if fd.new is None else "\n".join(apply_blocks(old, blocks))
            else:
                old = []
                new_text = "\n".join(fd.plus)
            if fd.new is not None:
                res.files[fd.new] = new_text
            if fd.old and fd.new and fd.old != fd.new:
                res.files[fd.old] = None  # rename: old path disappears

            if path.startswith("sqlglot/") and path.endswith(".py"):
                self._sqlglot_py(path, fd, blocks, old, exists, res)
            elif path.startswith("tests/") and path.endswith(".py"):
                self._test_py(path, fd, blocks, old, exists, res)
            else:
                ts = set(m.opened.get(path, []))
                if fd.old and fd.new and fd.old != fd.new:
                    ts |= set(m.opened.get(fd.new, []))
                if not ts and not exists:
                    res.notes.append(f"new-file:{path}")
                res.tests |= ts
                res.notes.append(f"data:{path}:{len(ts)}")
        return res

    def _sqlglot_py(self, path, fd, blocks, old, exists, res):
        m = self.m
        if not exists or path not in m.files:
            # new module: nothing imports it at the pinned commit unless discovered
            # dynamically (entry points / dialect enumeration). Run those users.
            ts = set(m.opened.get(os.path.dirname(path) + "/__init__.py", []))
            res.tests |= ts
            res.notes.append(f"new-module:{path}:{len(ts)}")
            return
        if fd.new is None:  # deleted module
            res.import_break.append(path)
            res.tests |= self._importers(path)
            return
        stmts = m.files[path]["stmts"]
        semantic, broken = self._semantic_blocks(path, stmts, old, blocks)
        if broken:
            res.import_break.append(path)
            res.tests |= self._importers(path)
            res.notes.append(f"syntax:{path}")
            return
        if not semantic:
            res.notes.append(f"noop:{path}")
            return
        lines = set()
        for b in semantic:
            if b.hi >= b.lo:
                lines.update(range(b.lo, b.hi + 1))
            else:
                lines.update((b.lo - 1, b.lo))
        ts, module = m.tests_for_lines(path, lines)
        res.tests |= ts
        res.notes.append(f"code:{path}:{len(ts)}{':module' if module else ''}")

    def _importers(self, path):
        if path in self.m.core:
            return set(self.all_tests)
        ts = set(self.m.opened.get(path, []))
        for f in self.m.family.get(path, [path]):
            if f in self.m.files:
                ts.update(self.m.files[f]["mod_tests"])
        return ts

    def _test_py(self, path, fd, blocks, old, exists, res):
        m = self.m
        in_file = [n for n, (f, _) in m.test_file_of.items() if f == path]
        if not exists or path not in m.test_files:
            res.tests |= set(in_file)
            res.notes.append(f"new-testfile:{path}")
            return
        semantic, broken = self._semantic_blocks(path, m.test_files[path]["stmts"], old, blocks)
        if not semantic and not broken:
            res.notes.append(f"noop:{path}")
            return
        users = set(in_file) | set(m.opened.get(path, []))
        if broken:
            res.tests |= users
            return
        hit = set()
        for b in semantic:
            lo, hi = (b.lo, b.hi) if b.hi >= b.lo else (b.lo - 1, b.lo)
            inside = [n for n in in_file if n in m.test_ranges
                      and m.test_ranges[n][0] <= lo and hi <= m.test_ranges[n][1]
                      and not (b.hi < b.lo and m.test_ranges[n][0] == b.lo)]
            if inside:
                hit.update(inside)
            else:  # helper / class-level / module-level code in the test file
                hit |= users
        res.tests |= hit
        res.notes.append(f"testcode:{path}:{len(hit)}")
