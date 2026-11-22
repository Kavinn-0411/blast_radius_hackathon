"""Patch -> candidate tests, from the precomputed map (no source parsing here)."""

import bisect
import re
from dataclasses import dataclass, field


@dataclass
class FileDiff:
    old: str | None  # path in a/ (None if new file)
    new: str | None  # path in b/ (None if deleted)
    removed: list[int] = field(default_factory=list)  # old line numbers removed
    inserted_at: list[int] = field(default_factory=list)  # old line before which lines were added
    minus: list[str] = field(default_factory=list)  # removed text
    plus: list[str] = field(default_factory=list)  # added text
    hunks: list[tuple[int, list[str]]] = field(default_factory=list)  # (old_start, raw lines)


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    old_ln = 0
    hunk_lines: list[str] | None = None
    old_left = new_left = 0
    for line in text.split("\n"):
        if hunk_lines is not None and (old_left > 0 or new_left > 0):
            # inside a hunk body
            tag = line[:1]
            if tag == "\\":
                continue
            hunk_lines.append(line)
            if tag == "-":
                cur.removed.append(old_ln)
                cur.minus.append(line[1:])
                old_ln += 1
                old_left -= 1
            elif tag == "+":
                cur.inserted_at.append(old_ln)
                cur.plus.append(line[1:])
                new_left -= 1
            else:  # context (" " or empty line in a stripped diff)
                old_ln += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            cur = FileDiff(None, None)
            files.append(cur)
            hunk_lines = None
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
        elif line.startswith("--- "):
            if cur is None:
                cur = FileDiff(None, None)
                files.append(cur)
            p = line[4:].split("\t")[0]
            cur.old = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            cur.new = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            old_ln = int(m.group(1))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            if old_left == 0:  # pure insertion: "-N,0" means after line N
                old_ln += 1
            hunk_lines = []
            cur.hunks.append((old_ln, hunk_lines))
    return files


class Map:
    def __init__(self, data: dict):
        self.files = data["files"]
        self.family = data["family"]
        self.opened = data["opened"]
        self.test_files = data.get("test_files", {})
        self.test_ranges = {int(k): v for k, v in data.get("test_ranges", {}).items()}
        self.core = set(data.get("core", []))
        self.test_file_of: dict[int, tuple[str, str]] = {}
        self._starts = {f: [u[0] for u in d["units"]] for f, d in self.files.items()}
        self._cstarts: dict = {}
        self.attr_xref = data.get("attr_xref", {})
        self.class_users: dict = {}
        for d in self.files.values():
            for (_, _, key), users in zip(d.get("classes", ()), d.get("class_tests", ())):
                self.class_users[key] = users

    def innermost(self, path: str, line: int) -> int:
        """Index of the innermost unit whose [start, end] holds line, or -1."""
        d = self.files[path]
        units = d["units"]
        i = bisect.bisect_right(self._starts[path], line) - 1
        best = -1
        # units are in pre-order; walk back over candidates that start <= line
        while i >= 0:
            s, _, e, _ = units[i]
            if s <= line <= e:
                best = i
                break
            i -= 1
        # pre-order guarantees the first containing unit found walking back is the
        # innermost one only if nested units follow parents; check deeper ones too
        return best

    def module_level(self, path: str) -> set[int]:
        """Everything that used the module's family: ran any of its lines or
        instantiated any of its classes."""
        out: set[int] = set()
        for f in self.family.get(path, [path]):
            if f in self.files:
                d = self.files[f]
                out.update(d["mod_tests"])
                for users in d.get("class_tests", ()):
                    out.update(users)
        return out

    def class_level(self, path: str, line: int) -> set[int] | None:
        """Tests for a change to a statement directly in a class body: users of
        that class (instantiated it or a subclass) plus users of classes whose
        body reads one of the names it assigns. None -> no usable class info."""
        d = self.files[path]
        cst = d.get("cstmts")
        if not cst:
            return None
        if path not in self._cstarts:
            self._cstarts[path] = [c[0] for c in cst]
        i = bisect.bisect_right(self._cstarts[path], line) - 1
        best = None
        while i >= 0:
            s, e, ci, names = cst[i]
            if s <= line <= e and (best is None or s >= best[0]):
                best = cst[i]
                break  # sorted by start: first hit walking back is innermost
            i -= 1
        if best is None:
            # a line inside a class but between its statements: that class
            inner = None
            for k, (s, e, _) in enumerate(d.get("classes", ())):
                if s < line <= e and (inner is None or s >= d["classes"][inner][0]):
                    inner = k
            if inner is None:
                return None
            best = (0, 0, inner, [])
        _, _, ci, names = best
        users = d["class_tests"][ci]
        if not users:
            return None
        out = set(users)
        qual = self._class_key(path, ci).split(":", 1)[1].split(".")
        for n in names:
            for key, chain in self.attr_xref.get(n, ()):
                # body of `key` reads <chain>.n; keep it if chain can name this class
                c = chain.split(".") if chain else []
                k = min(len(c), len(qual))
                if k and c[-k:] == qual[-k:]:
                    out.update(self.class_users.get(key, ()))
        return out

    def _class_key(self, path: str, ci: int) -> str:
        return self.files[path]["classes"][ci][2]

    def tests_for_lines(self, path: str, lines: set[int]):
        """(tests, touched_module_level, groups). Each group is the test set one
        changed line maps to (its innermost unit / class / module); used as
        evidence for giant tests."""
        d = self.files[path]
        out: set[int] = set()
        groups: list[set[int]] = []
        module = False
        for ln in lines:
            g: set[int] = set()
            u = self.innermost(path, ln)
            first = True
            while u >= 0:
                s, body, e, parent = d["units"][u]
                if first:
                    g.update(d["unit_tests"][u])
                    first = False
                out.update(d["unit_tests"][u])
                if ln >= body:
                    break
                u = parent  # signature/decorator line: definition-time effect
            if u < 0:
                # class/module level: users are a broad superset, too weak to
                # vouch for a giant test -> contributes no evidence group
                g = None
                cl = self.class_level(path, ln)
                if cl is None:
                    module = True
                else:
                    out |= cl
            if g:
                groups.append(g)
        if module:
            out |= self.module_level(path)
        return out, module, groups
