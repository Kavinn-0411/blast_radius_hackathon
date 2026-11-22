"""What to run ourselves, what to select blind, and what to select afterwards."""

from dataclasses import dataclass, field

from pack import pack

GIANT_S = 1.0  # never run tests at least this long ourselves
VERIFY_OVERHEAD_S = 0.6  # fork + import patched sqlglot + collect (CPU)
VERIFY_FACTOR = 1.3  # our CPU per recorded test second
GATE_GIANTS = True  # drop a giant when cheap tests covering the same change all pass
GATE_MODE = "evidence"  # "evidence": per-giant evidence; "any": keep all if anything failed
# isolated single-test measurements double-count shared imports; real jobs on the
# sample cost ~0.5x the raw sum (fit on 20 jobs, 2026-11-21)
CAL = 0.55
MAX_VERIFY_S = 8.0  # never spend more than this much estimated CPU on one commit
POOL_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")

COST: dict = {}  # test -> measured marginal CPU seconds under our harness (from the map)
FILE_COST: dict = {}  # test file -> fixed CPU to collect/import it under our harness
CLASS_COST: dict = {}  # "file::Class" -> setUpClass etc.
SESSION: list = [VERIFY_OVERHEAD_S]  # per-job fixed CPU (pytest session + sqlglot import)
# CPU seconds on this machine per CPU second on the machine the costs were
# measured on (set at start from a short benchmark)
SPEED: list = [1.0]


def cost_s(t: int, sec: dict, file_of: dict) -> float:
    """Estimated CPU for running test t ourselves."""
    if t in COST:
        return COST[t]
    # recorded seconds are wall time; pool-based files did their work in workers
    f = file_of.get(t, ("", ""))[0]
    return sec[t] * (10.0 if f in POOL_FILES else VERIFY_FACTOR)


def estimate(tests, sec: dict, file_of: dict) -> float:
    """Estimated CPU of one job running these tests."""
    files, classes = set(), set()
    for t in tests:
        tid = file_of.get(t, ("", ""))[1]
        files.add(tid.split("::")[0])
        classes.add("::".join(tid.split("::")[:-1]))
    raw = (SESSION[0] + sum(FILE_COST.get(f, 0.3) for f in files)
           + sum(CLASS_COST.get(c, 0.0) for c in classes)
           + sum(cost_s(t, sec, file_of) for t in tests))
    return CAL * raw


@dataclass
class Plan:
    blind: set = field(default_factory=set)  # selected without running
    check: set = field(default_factory=set)  # run ourselves
    gated: dict = field(default_factory=dict)  # giant -> evidence (cheap tests)
    est_s: float = 0.0  # estimated CPU of the check run


def make_plan(res, sec: dict, ms: dict, known: set, file_of: dict | None = None,
              budget_left: float = 1e9, data_only: bool = False) -> Plan:
    cand = res.tests & known
    file_of = file_of or {}
    giants = {t for t in cand if sec[t] >= GIANT_S or cost_s(t, sec, file_of) >= GIANT_S * 2}
    cheap = cand - giants
    p = Plan()
    if not cheap:
        p.blind = cand
        return p
    gated = {}
    if GATE_GIANTS:
        for g in giants:
            ev = set()
            for grp in res.groups:
                if g in grp:
                    ev |= grp & cheap
            if ev:
                gated[g] = ev
    must = giants - set(gated)
    _, cost_blind = pack(sorted(cand), ms)
    _, cost_must = pack(sorted(must), ms)
    est_s = estimate(cheap, sec, file_of)
    est = est_s * SPEED[0] * 1000
    if data_only and (len(cheap) <= 3 or cost_blind <= 3500):
        # an edited fixture almost always breaks the tests reading it: a short
        # run would cost CPU only to confirm that
        p.blind = cand
        return p
    if est + cost_must >= cost_blind or est_s > MAX_VERIFY_S or est_s > budget_left:
        p.blind = cand  # running would cost more than selecting everything blind
        return p
    p.blind, p.check, p.gated = must, cheap, gated
    p.est_s = est_s
    return p


def finish(p: Plan, failed: set) -> set:
    """Selection once the checked tests' failures are known."""
    chosen = set(p.blind) | (failed & p.check)
    for g, ev in p.gated.items():
        if GATE_MODE == "any":
            if failed:
                chosen.add(g)
        elif ev & failed:
            chosen.add(g)
    return chosen
