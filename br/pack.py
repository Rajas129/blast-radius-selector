"""Split a selection over at most 8 workers, in whole milliseconds like the scorer."""

WORKER_MS = 2000


def bound(total, big, n):
    """Cost of a perfect split: a cheap stand-in for pack() inside decision loops."""
    if n == 0:
        return 0
    best = None
    for k in range(1, 9):
        c = max(big, -(-total // k)) + WORKER_MS * k
        if best is None or c < best:
            best = c
    return best


def bound_of(ids, ms):
    t = b = 0
    for i in ids:
        v = ms[i]
        t += v
        if v > b:
            b = v
    return bound(t, b, len(ids))


def _lpt(order, ms, k):
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for i in order:
        j = loads.index(min(loads))
        bins[j].append(i)
        loads[j] += ms[i]
    return bins, loads


def _improve(bins, loads, ms):
    for _ in range(200):
        hi = loads.index(max(loads))
        lo = loads.index(min(loads))
        if hi == lo:
            break
        gap = loads[hi] - loads[lo]
        best = None
        for a in bins[hi]:
            if 0 < ms[a] < gap:
                gain = min(ms[a], gap - ms[a])
                if best is None or gain > best[0]:
                    best = (gain, a, None)
            for b in bins[lo]:
                d = ms[a] - ms[b]
                if 0 < d < gap:
                    gain = min(d, gap - d)
                    if best is None or gain > best[0]:
                        best = (gain, a, b)
            if len(bins[hi]) * len(bins[lo]) > 4000:
                break
        if best is None:
            break
        _, a, b = best
        bins[hi].remove(a)
        bins[lo].append(a)
        loads[hi] -= ms[a]
        loads[lo] += ms[a]
        if b is not None:
            bins[lo].remove(b)
            bins[hi].append(b)
            loads[lo] -= ms[b]
            loads[hi] += ms[b]
    return bins, loads


def pack(ids, ms, extra=()):
    """ids: tests to run. extra: dropped candidates to slip into idle time for free."""
    ids = sorted(set(ids), key=lambda i: (-ms[i], i))
    if not ids:
        return []
    total = sum(ms[i] for i in ids)
    big = ms[ids[0]]
    best = None
    for k in range(1, min(8, len(ids)) + 1):
        if best is not None and max(big, -(-total // k)) + WORKER_MS * k >= best[0]:
            continue
        bins, loads = _lpt(ids, ms, k)
        if k > 1 and max(loads) > max(big, -(-total // k)):
            bins, loads = _improve(bins, loads, ms)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins, loads)
    _, bins, loads = best
    top = max(loads)
    if extra and len(bins) > 1:
        chosen = set(ids)
        for i in sorted(set(extra) - chosen, key=lambda i: (ms[i], i)):
            j = loads.index(min(loads))
            if loads[j] + ms[i] > top:
                break
            bins[j].append(i)
            loads[j] += ms[i]
    return [b for b in bins if b]
