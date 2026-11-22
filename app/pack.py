"""Split tests over <= 8 workers minimising (busiest load + 2 s per worker)."""

WORKER_MS = 2000
MAX_WORKERS = 8


def pack(tests: list[int], ms: dict[int, int]) -> tuple[list[list[int]], int]:
    if not tests:
        return [], 0
    order = sorted(tests, key=lambda t: -ms[t])
    total = sum(ms[t] for t in order)
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        if best is not None and max(ms[order[0]], total / k) + WORKER_MS * k >= best[0]:
            break  # lower bound for this and larger k cannot win
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for t in order:  # longest processing time first
            i = loads.index(min(loads))
            loads[i] += ms[t]
            bins[i].append(t)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1], best[0]
