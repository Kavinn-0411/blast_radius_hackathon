"""Did a long test reach the edited statements? (line coverage at the pinned commit)

A long test that never executed an edited statement cannot behave differently
after the edit: everything it ran before reaching that point is unchanged, and
it never reaches it. Used only for edits inside function bodies; anything else
(class bodies, module level, signatures, fixtures, test files) keeps the
broader selection.
"""


class Reach:
    def __init__(self, giant_lines: dict, files: dict):
        self.files = files
        self.cov: dict[int, dict[str, set]] = {}
        for t, fl in giant_lines.items():
            self.cov[int(t)] = {
                f: {x for a, b in rs for x in range(a, b + 1)} for f, rs in fl.items()}

    def _span(self, path: str, ln: int):
        """Lines that run whenever the smallest statement holding ln runs: the
        whole simple statement, or the header of a compound one."""
        best = None
        for s, hdr, e in self.files[path]["stmts"]:
            if s <= ln <= e and (best is None or e - s < best[2] - best[0]):
                best = (s, hdr, e)
        if best is None:
            return None
        s, hdr, e = best
        return s, hdr

    def reached(self, t: int, edits) -> bool:
        cov = self.cov.get(t)
        if cov is None:
            return True  # no coverage on record: assume it is affected
        for path, lines in edits:
            fc = cov.get(path)
            if not fc:
                continue
            for ln in lines:
                sp = self._span(path, ln)
                if sp is None:
                    return True
                if any(x in fc for x in range(sp[0], sp[1] + 1)):
                    return True
        return False
