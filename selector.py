"""Blast Radius selector.

For every patch: pick the tests the change can reach (maps of the pinned
commit), then check the cheap ones against the patched code in a forked child
and keep only what fails.  The five heavy tests are never run; each has a
cheap proof or stand-in.  Anything that cannot be settled stays selected.
"""
import json
import os
import pickle
import select
import signal
import sys
import time

T0 = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = os.environ.get("BR_DATA", "/data")
ROOT = os.path.realpath(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "maps"))
WALL = float(os.environ.get("BR_WALL", "150"))   # stop starting children after this
PRICE_FUDGE = 1.1


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bench():
    best = 9.0
    for _ in range(6):
        t = time.process_time()
        x = 0
        for i in range(150000):
            x += i % 7
        d = {}
        for i in range(30000):
            d[str(i)] = i
        best = min(best, time.process_time() - t)
    return best


def emit(commit, workers):
    sys.stdout.write(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n")


def everything(tests):
    from br import pack
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    return pack.pack(list(ms), ms)


def run_child(R, plan, limit):
    """Fork, let the child check candidates, collect what it reports."""
    from br import child
    R.prepare(plan)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            child.child_main(R, plan, w)
        except BaseException as e:  # noqa
            code = 1
            try:
                os.write(2, ("  child error: %r\n" % (e,)).encode())
            except OSError:
                pass
        finally:
            os._exit(code)
    os.close(w)
    buf = bytearray()
    end = time.time() + limit
    killed = False
    while True:
        left = end - time.time()
        if left <= 0:
            killed = True
            break
        ready, _, _ = select.select([r], [], [], min(left, 1.0))
        if ready:
            chunk = os.read(r, 1 << 16)
            if not chunk:
                break
            buf += chunk
    if killed:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    os.close(r)
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    import io
    msgs = []
    bio = io.BytesIO(bytes(buf))
    while True:
        try:
            msgs.append(pickle.load(bio))
        except Exception:
            break
    return msgs, killed


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    cdir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    done = set()
    try:
        solve(tests, cdir, commits, done)
    except BaseException as e:  # never crash: a slow answer beats a rejected run
        log("fatal, falling back:", repr(e))
        full = everything(tests)
        for c in commits:
            if c not in done:
                emit(c, full)
    sys.stdout.flush()


def solve(tests, cdir, commits, done):
    from br import analyze, pack
    maps = analyze.Maps(MAPS, ROOT)
    meta = maps.meta
    N = maps.N
    number = [None] * N          # map index -> number in tests.json
    ms_num = {}
    unknown = []
    for t in tests:
        ms_num[t["number"]] = round(t["seconds"] * 1000)
        i = maps.index.get(t["id"])
        if i is None:
            unknown.append(t["number"])
        else:
            number[i] = t["number"]
    ms = [ms_num[number[i]] if number[i] is not None else 0 for i in range(N)]
    full = pack.pack(list(ms_num), ms_num)

    def out(commit, selected_bits, extra_bits=0, force_unknown=True):
        nums = [number[i] for i in bits(selected_bits) if i < N and number[i] is not None]
        if nums or (unknown and force_unknown and selected_bits):
            nums += unknown
        fill = [number[i] for i in bits(extra_bits) if i < N and number[i] is not None]
        emit(commit, pack.pack(nums, ms_num, fill))
        done.add(commit)

    from br.child import bits
    plans = {}
    dynamic = []
    for c in commits:
        try:
            with open(os.path.join(cdir, c + ".patch"), encoding="utf-8",
                      errors="surrogateescape", newline="") as fh:
                pl = analyze.plan_commit(maps, fh.read())
        except Exception as e:
            log(c, "analysis failed:", repr(e))
            emit(c, full)
            done.add(c)
            continue
        plans[c] = pl
        if pl.all:
            log(c, "config change: everything")
            emit(c, full)
            done.add(c)
        elif not ((pl.mask & maps.REAL) or pl.reimport):
            # nothing left to check: a code swap alone has nothing to run
            log(c, "static: %d tests" % bin(pl.keep & maps.REAL).count("1"))
            out(c, pl.keep)
        else:
            dynamic.append(c)
    if not dynamic:
        return
    # one CPU second here, in the reference machine's seconds the cost estimates use
    price = PRICE_FUDGE * max(0.9, min(1.8, bench() / meta["bench"]))
    if os.environ.get("BR_PRICE"):
        price = float(os.environ["BR_PRICE"])
    from br.child import Runner
    R = Runner(maps, ms, price)
    log("warm parent ready, price %.2f, %d commits to check, %.2fs cpu" % (
        price, len(dynamic), time.process_time()))
    for n, c in enumerate(dynamic):
        try:
            sys.stdout.flush()
            check(R, maps, plans[c], c, len(dynamic) - n, out, full, done)
        except Exception as e:
            log(c, "check failed:", repr(e))
            if c not in done:
                emit(c, full)
                done.add(c)


def check(R, maps, pl, c, remaining, out, full, done):
    elapsed = time.time() - T0
    msgs, killed = [], False
    if elapsed < WALL:
        share = (WALL + 20 - elapsed) / max(1, remaining)
        msgs, killed = run_child(R, pl, max(8.0, min(40.0, share * 3)))
    M = None
    passed = 0
    all_fail = False
    for m in msgs:
        if m[0] == "M":
            M = m[1]
        elif m[0] == "p":
            passed |= 1 << m[1]
        elif m[0] == "all":
            all_fail = True
    if all_fail:
        log(c, "sqlglot no longer imports: everything")
        emit(c, full)
        done.add(c)
        return
    if M is None:
        if pl.reimport or pl.swaps:
            # the child died before it could say what the change reaches
            log(c, "no verdict from child: everything")
            emit(c, full)
            done.add(c)
            return
        M = pl.mask
    sel = ((M & ~passed) | pl.keep) & maps.REAL
    log(c, "candidates %d, passed %d, selected %d%s" % (
        bin(M & maps.REAL).count("1"), bin(passed).count("1"), bin(sel).count("1"),
        " (child killed)" if killed else ""))
    out(c, sel, pl.wide & ~passed & ~sel)


if __name__ == "__main__":
    main()
