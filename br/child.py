"""Runs inside a forked child: load the patched code, then check candidates."""
import gc
import importlib
import os
import pickle
import sys
import time
import types

from br import heavy, overlay, pack, shim, state, testrun
from br.analyze import users

PASS_P = 0.5       # prior that a set of cheap candidates all pass
HEAVY_MARGIN = 0.7
NP_TESTS = {"test_convert", "test_sql_boolean_logic_with_numpy_scalars"}


def bits(m):
    out = []
    i = 0
    while m:
        low = m & -m
        i = low.bit_length() - 1
        out.append(i)
        m ^= low
    return out


def log(*a):
    try:
        os.write(2, (" ".join(str(x) for x in a) + "\n").encode())
    except OSError:
        pass


class Runner:
    """Parent-side state shared with every child through fork."""

    def __init__(self, maps, ms, price):
        self.maps, self.ms, self.price = maps, ms, price
        self.root = maps.root
        os.environ["SKIP_INTEGRATION"] = "0"
        if self.root not in sys.path:
            sys.path.insert(0, self.root)
        self.mods = []
        for name in maps.meta["mods"]:
            try:
                importlib.import_module(name)
                self.mods.append(name)
            except BaseException:
                pass
        self._fidx = None
        self._base = {}
        self.have_duck = importlib.util.find_spec("duckdb") is not None \
            and importlib.util.find_spec("pandas") is not None
        self.have_numpy = importlib.util.find_spec("numpy") is not None
        meta = maps.meta
        self.heavy = {}
        self.tests = []
        for i, tid in enumerate(meta["ids"]):
            f, c, m = tid.split("::")
            self.tests.append((f, c, m))
            if (c, m) in heavy.NEVER_RUN:
                self.heavy[i] = (c, m)

    def func_index(self):
        if self._fidx is None:
            idx = {}
            pre = self.root + os.sep
            for o in gc.get_objects():
                if type(o) is types.FunctionType:
                    c = o.__code__
                    if c.co_filename.startswith(pre):
                        idx.setdefault((c.co_filename, c.co_firstlineno, c.co_name), []).append(o)
            self._fidx = idx
        return self._fidx

    def prepare(self, plan):
        """Work that must happen before the fork so later children inherit it."""
        if plan.swaps:
            self.func_index()
        plan.deps = []
        if plan.state:
            self.maps.inst, self.maps.ident
            plan.deps = state.dependents(set(plan.changed_mods), self.mods)
            missing = [m for m in plan.deps if m not in self._base]
            if missing:
                snap = state.snapshot(missing)
                for m in missing:
                    self._base[m] = {}
                for k, v in snap.items():
                    self._base[k[0]][k] = v


def _find_code(top, name, line):
    stack = [top]
    while stack:
        c = stack.pop()
        for k in c.co_consts:
            if type(k) is types.CodeType:
                if k.co_name == name and k.co_firstlineno == line:
                    return k
                stack.append(k)
    return None


def swap_all(R, plan):
    idx = R.func_index()
    tops = {}
    todo = []
    for path, oline, name, nline, rel in plan.swaps:
        funcs = idx.get((path, oline, name))
        if not funcs:
            return False
        if rel not in tops:
            tops[rel] = compile(plan.files[rel], path, "exec", dont_inherit=True)
        new = _find_code(tops[rel], name, nline)
        if new is None:
            return False
        for f in funcs:
            if new.co_freevars != f.__code__.co_freevars:
                return False
        todo.append((funcs, new))
    for funcs, new in todo:
        for f in funcs:
            f.__code__ = new
    return True


def reimport(R, extra):
    old = {}
    for k in list(sys.modules):
        if k == "sqlglot" or k.startswith("sqlglot.") or k == "tests" or k.startswith("tests."):
            old[k] = sys.modules.pop(k)
    fails = []
    lazy_ok = True
    hung = 0
    for name in R.mods + extra:
        testrun.arm(2.0)   # the whole package imports in a fraction of a second
        try:
            m = importlib.import_module(name)
            if name == "sqlglot":
                # what test_lazy_load checks in a fresh interpreter
                lazy_ok = len(m.Dialect._classes) == 1
        except testrun.Timeout:
            fails.append(name)
            hung += 1
            if name == "sqlglot" or hung >= 3:
                testrun.disarm()
                return ["sqlglot"], old, lazy_ok
        except BaseException:
            fails.append(name)
            if name == "sqlglot":
                break
        finally:
            testrun.disarm()
    return fails, old, lazy_ok


def _class_of(mods, mn, q):
    m = mods.get(mn)
    o = m
    try:
        for part in q.split("."):
            o = getattr(o, part)
        return o if isinstance(o, type) else None
    except AttributeError:
        return None


def state_changes(R, plan, old, fails=()):
    maps = R.maps
    base = {}
    for m in plan.deps:
        base.update(R._base.get(m, {}))
    now = state.snapshot(plan.deps + [m for m in plan.changed_mods if m not in plan.deps])
    changes = state.diff(base, now, maps.meta.get("noisy", ()))
    # a module that no longer imports is handled through the tests that load it
    changes = [c for c in changes if c[0] not in fails and c[0] in sys.modules]
    tracked = maps.meta["tracked"]
    info = {}
    exprs = None
    for mn, q, attr, kind, entries in changes:
        if not q:
            continue
        key = (mn, q)
        if key not in info:
            cls = _class_of(sys.modules, mn, q) or _class_of(old, mn, q)
            ikey = None
            if cls is not None and any((k.__module__ + ":" + k.__qualname__) in tracked
                                       for k in cls.__mro__):
                ikey = mn + ":" + q
            info[key] = (ikey, {})
        if kind == "callable" and attr.endswith("_sql"):
            if exprs is None:
                exprs = {}
                for m2 in (sys.modules, old):
                    for name, mod in list(m2.items()):
                        if name.startswith("sqlglot.expressions"):
                            for v in list(vars(mod).values()):
                                if isinstance(v, type) and isinstance(getattr(v, "key", None), str):
                                    exprs.setdefault(v.key, set()).add(
                                        v.__module__ + ":" + v.__qualname__)
            info[key][1][attr] = sorted(exprs.get(attr[:-4], ()))
    resized = set()
    for mn, q, attr, kind, entries in changes:
        a, b = base.get((mn, q, attr)), now.get((mn, q, attr))
        if a and b and a[1] is not None and b[1] is not None and a[1].keys() != b[1].keys():
            resized.add((mn, q, attr))
    return changes, users(maps, changes, info, resized)


def child_main(R, plan, w):
    maps = R.maps
    meta = maps.meta
    ms = R.ms
    N = maps.N
    price = R.price
    t_begin = time.process_time()

    def send(*o):
        os.write(w, pickle.dumps(o))

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    errfd = os.dup(2)
    os.dup2(devnull, 2)

    def say(*a):
        try:
            os.write(errfd, ("  child: " + " ".join(str(x) for x in a) + "\n").encode())
        except OSError:
            pass

    files = dict(plan.files)
    standin_ok = {}
    scope = None
    genmem_scale = 1.0
    try:
        if not any(r.startswith("tests/") for r in plan.files):
            scope = heavy.dialect_scope(plan.changed_mods, R.mods)
            if scope is not None:
                genmem_scale = min(1.0, (len(scope) + 1) / 30.0)
    except Exception:
        scope = None
    for rel in set(heavy.STANDIN.values()):
        src = files[rel] if rel in files else maps.old_text(rel)
        t = heavy.standin_text(rel, src, scope) if src is not None else None
        standin_ok[rel] = t is not None
        if t is not None:
            files[rel] = t
    overlay.install(R.root, files)
    testrun.guard_spawn()

    M = plan.mask
    old = {}
    need_re = plan.reimport
    if not need_re and plan.swaps:
        try:
            if not swap_all(R, plan):
                need_re = True
        except Exception:
            need_re = True
        if need_re:
            say("swap fell back to reimport")
    fails = []
    if need_re:
        new_mods = [r[:-3].replace("/", ".") for r, t in plan.files.items()
                    if r.startswith("sqlglot/") and r.endswith(".py") and t is not None
                    and not r.endswith("__init__.py")]
        try:
            fails, old, lazy_ok = reimport(R, [m for m in new_mods if m not in R.mods])
            if not lazy_ok:
                i = maps.index.get("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
                if i is not None:
                    M |= 1 << i
        except testrun.Timeout:
            fails = ["sqlglot"]
        testrun.disarm()
        if "sqlglot" in fails:
            send("all")
            return
        for f in fails:
            # tests that import it, and tests that build one of its classes
            # (dialect modules are loaded lazily by name)
            M |= meta["reqmap"].get(f, 0)
            pre = f + ":"
            for k, v in maps.inst.items():
                if k.startswith(pre):
                    M |= v
        if fails:
            say("import failures:", fails[:6])
    if plan.state and "sqlglot" not in fails:
        changes, um = state_changes(R, plan, old, fails)
        say("state changes:", len(changes), [c[:4] for c in changes[:6]], "users", bin(um).count("1"))
        M |= um
    send("M", M)

    real = [i for i in bits(M) if i < N]
    if not real:
        return
    kept = plan.keep & maps.REAL
    alive = set(real) | set(bits(kept))   # everything not proven to pass, plus what is kept anyway
    use_real_duck = False

    def bound_alive(without=()):
        return pack.bound_of([i for i in alive if i not in without], ms)

    def norun(i):
        if not meta["ok"][i] or meta["spawn"][i]:
            return True
        if meta["duck"][i] and not R.have_duck:
            return True
        if R.tests[i][2] in NP_TESTS and not R.have_numpy:
            return True
        if R.tests[i][0] == "tests/test_executor.py" and "tests/test_executor.py" in plan.files:
            return True
        return False

    loaded = {}
    setup_done = {}
    setup_bit_exec = maps.bit("setup:tests/test_executor.py::TestExecutor")

    def get_class(i):
        f, c, m = R.tests[i]
        if f not in loaded:
            try:
                loaded[f] = testrun.load_module(f)
            except testrun.Timeout:
                raise
            except BaseException:
                loaded[f] = None
        mod = loaded[f]
        if mod is None:
            return None
        cls = getattr(mod, c, None)
        if cls is None:
            return None
        key = (f, c)
        if key not in setup_done:
            if c == "TestExecutor" and not (M & setup_bit_exec):
                setup_done[key] = True   # nothing this change reaches runs in its setUpClass
            else:
                setup_done[key] = testrun.class_setup(cls)
        return cls if setup_done[key] else None

    nonrt = plan.other | (M & ~plan.mask)
    filt = {}

    def case_filter(i):
        """Fixture cases of test i that can reach the changed lines, or None for all."""
        if plan.state or (nonrt >> i) & 1 or i not in meta["ncases"]:
            return None
        F = {}
        for rel, lines in plan.rt.items():
            uniq, packed = maps.cov(rel)
            cm, nc = maps.case(rel)
            for ln in lines:
                k = packed.get(ln)
                if k is None or not (uniq[k] >> i) & 1:
                    continue
                if (nc.get(ln, 0) >> i) & 1:
                    return None
                per = cm.get(ln, {}).get(i)
                if per is None:
                    return None
                for f, m in per.items():
                    F[f] = F.get(f, 0) | m
        return F or None

    def run_one(i, limit):
        shim.USED[0] = 0
        testrun.SPAWNED[0] = 0
        ok = False
        testrun.arm(limit)
        try:
            cls = get_class(i)
            ok = cls is not None and testrun.run_case(cls, R.tests[i][2], filt.get(i))
        except testrun.Timeout:
            ok = False
        except BaseException:
            ok = False
        finally:
            testrun.disarm()
        if ok and ((shim.USED[0] and not use_real_duck) or testrun.SPAWNED[0]):
            return None   # verdict cannot be trusted
        return ok

    def install_shims(want_numpy):
        if use_real_duck:
            return
        shim.install(numpy=want_numpy and R.have_numpy)

    # ---- heavy tests: proofs and stand-ins -------------------------------
    heavies = sorted([i for i in real if i in R.heavy and not (kept >> i) & 1], key=lambda i: -ms[i])
    digests = {}

    def tpc_plan(i):
        c, m = R.tests[i][1], R.tests[i][2]
        if "tests/test_executor.py" in plan.files or "tests/helpers.py" in plan.files:
            return None
        if M & setup_bit_exec:
            return None
        if m == "test_execute_tpcds":
            block, pre = maps.bit("B:ds"), ("A:ds:",)
        elif m == "test_execute_tpch":
            block, pre = maps.bit("B:h"), ("A:h:",)
        else:
            block, pre = 0, ("A:h:", "O:h:")
        if M & block:
            return None
        names = [n for n, b in maps.pseudo.items() if n.startswith(pre) and (M >> b) & 1]
        if not names or any(n not in meta["gold"] for n in names):
            return None
        return names

    shim_ready = [False]
    for h in heavies:
        c, m = R.heavy[h]
        gain = bound_alive() - bound_alive((h,))
        names = None
        if (c, m) in heavy.STANDIN:
            rel = heavy.STANDIN[(c, m)]
            okb, cost = meta["stand"].get((c, m), (False, 0))
            can = okb and standin_ok.get(rel)
            if m == "test_generator_memory":
                cost *= genmem_scale
        else:
            names = tpc_plan(h)
            can = names is not None
            cost = sum(meta["gold"][n][1] for n in names if n not in digests) if can else 0
        if not can or cost * price >= HEAVY_MARGIN * gain / 1000.0:
            say("heavy kept without check:", m, "can" if can else "cannot", "gain %.1f" % (gain / 1000.0))
            continue
        if not shim_ready[0]:
            install_shims(False)
            shim_ready[0] = True
        t0 = time.process_time()
        if names is None:
            ok = run_one(h, max(3.0, 12 * cost))
        else:
            ok = True
            testrun.arm(max(3.0, 12 * cost))
            try:
                for n in names:
                    if n not in digests:
                        digests[n] = heavy.digest(n) == meta["gold"][n][0]
                    if not digests[n]:
                        ok = False
                        break
            except testrun.Timeout:
                ok = False
            except BaseException:
                ok = False
            finally:
                testrun.disarm()
        say("heavy", m, "passes" if ok else "kept", "%.2fs" % (time.process_time() - t0))
        if ok:
            alive.discard(h)
            send("p", h)

    # ---- cheap candidates -------------------------------------------------
    # a test selected anyway (it reads a patched file) gains nothing from a run
    cheap = [i for i in real if i not in R.heavy and not norun(i) and not (kept >> i) & 1]
    if not cheap:
        return
    cost = list(meta["cost"])
    for i in cheap:
        if cost[i] >= 0.1:
            F = case_filter(i)
            if F is not None:
                tot = sum(meta["ncases"][i].values()) or 1
                part = sum(bin(m).count("1") for m in F.values())
                filt[i] = F
                cost[i] = cost[i] * min(1.0, part / tot) + 0.01
    hint = plan.hint

    def order_key(i):
        f = R.tests[i][0]
        near = 0 if (f in plan.test_edit or any(h in f for h in hint)) else 1
        return (near, cost[i], i)

    def estimate(group):
        est = sum(cost[i] for i in group) + 0.02
        if R.have_duck and any(meta["duck"][i] for i in group):
            est += 0.5
        for key, v in meta["setup_cost"].items():
            if v > 0.05 and (M >> maps.pseudo.get("setup:" + key, 10 ** 6)) & 1:
                est += v
        return est

    cheap.sort(key=order_key)
    est = estimate(cheap)
    gain0 = (bound_alive() - bound_alive(set(cheap))) / 1000.0
    if est * price >= PASS_P * gain0:
        # second chance: leave out tests that cost more to run than to select
        sub = [i for i in cheap if cost[i] * price < 1.5 * ms[i] / 1000.0 + 0.3]
        est2 = estimate(sub)
        gain2 = (bound_alive() - bound_alive(set(sub))) / 1000.0
        if not sub or len(sub) == len(cheap) or est2 * price >= PASS_P * gain2:
            say("not running %d cheap tests: est %.2fs cpu, gain %.2fs" % (len(cheap), est, gain0))
            return
        cheap, est, gain0 = sub, est2, gain2
    want_duck = R.have_duck and any(meta["duck"][i] for i in cheap)
    use_real_duck = want_duck
    if use_real_duck:
        for k in [k for k in sys.modules if getattr(sys.modules[k], "__br_shim__", False)]:
            del sys.modules[k]
    else:
        install_shims(any(R.tests[i][2] in NP_TESTS for i in cheap))
    ceiling = max(0.5 * gain0 / price, 0.3)
    t0 = time.process_time()
    ran = failed = 0
    left = set(cheap)
    rem = est
    for n, i in enumerate(cheap):
        if ran >= 8 and failed == ran:
            say("blast: everything fails, stopping")
            break
        if failed or n % 16 == 0:
            g = (bound_alive() - bound_alive(left)) / 1000.0
            p = (failed + 0.5) / (ran + 2.0) if failed else 0.0
            if n and rem * price >= (1 - p) * g:
                say("stop after %d: %.2fs left to run, %.2fs left to win" % (ran, rem, g))
                break
            if time.process_time() - t0 > ceiling:
                say("cpu ceiling reached after %d" % ran)
                break
        ok = run_one(i, max(1.0, 15 * cost[i] + 0.5))
        left.discard(i)
        rem -= cost[i]
        if ok is None:
            continue
        ran += 1
        if ok:
            alive.discard(i)
            send("p", i)
        else:
            failed += 1
    say("ran %d cheap, %d failed, %.2fs cpu total, est %.2f, run %.2f" % (
        ran, failed, time.process_time() - t_begin, est, time.process_time() - t0))
