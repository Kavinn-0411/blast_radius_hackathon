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
        out: set[int] = set()
        for f in self.family.get(path, [path]):
            if f in self.files:
                out.update(self.files[f]["mod_tests"])
        return out

    def tests_for_lines(self, path: str, lines: set[int]) -> tuple[set[int], bool]:
        """(tests, touched_module_level)."""
        d = self.files[path]
        out: set[int] = set()
        module = False
        for ln in lines:
            u = self.innermost(path, ln)
            while u >= 0:
                s, body, e, parent = d["units"][u]
                out.update(d["unit_tests"][u])
                if ln >= body:
                    break
                u = parent  # signature/decorator line: definition-time effect
            if u < 0:
                module = True
        if module:
            out |= self.module_level(path)
        return out, module
