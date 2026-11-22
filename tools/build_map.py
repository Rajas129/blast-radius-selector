"""Build the maps of the pinned sqlglot commit.

    python tools/build_map.py trace  <sqlglot_root> <tests.json> <out_dir>
    python tools/build_map.py plain  <sqlglot_root> <tests.json> <out_file>
    python tools/build_map.py merge  <sqlglot_root> <out_dir> <plain1> <plain2>

trace: runs every test once in this process under sys.monitoring and records,
       per test: function lines run, files opened, folders listed, classes
       instantiated and modules imported.
plain: runs every test without instrumentation: verdict, CPU cost, golden
       digests for the TPC stand-ins.  Run twice with different hash seeds.
merge: folds everything into maps/*.bin.

Everything here is derived from the pinned commit only.
"""
import ast
import builtins
import hashlib
import importlib
import io
import json
import os
import pickle
import pkgutil
import sys
import time
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

mode, ROOT = sys.argv[1], os.path.realpath(sys.argv[2])
sys.path.insert(0, ROOT)
os.environ["SKIP_INTEGRATION"] = "0"
PRE = ROOT + os.sep

from br import shim, testrun, heavy  # noqa: E402

try:
    if os.environ.get("BR_SHIM"):
        raise ImportError
    import duckdb  # noqa: F401

    REAL_DUCK = True
except Exception:
    REAL_DUCK = False
    shim.install(numpy=True)


def rel(p):
    return p[len(PRE):] if p.startswith(PRE) else None


def load_tests(path):
    tests = json.load(open(path))["tests"]
    out = []
    for t in tests:
        f, c, m = t["id"].split("::")
        out.append((f, c, m, t["seconds"]))
    return out


def groups(tests):
    """[(file, [(cls, [(idx, method)])])] in first-seen order."""
    files = {}
    for i, (f, c, m, _) in enumerate(tests):
        files.setdefault(f, {}).setdefault(c, []).append((i, m))
    return [(f, list(cs.items())) for f, cs in files.items()]


def import_everything():
    import sqlglot

    names = ["sqlglot"]
    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
            names.append(info.name)
        except Exception as e:  # optional deps
            print("skip module", info.name, repr(e)[:80], file=sys.stderr)
    return [n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")]


def bench():
    best = 9.0
    for _ in range(5):
        t = time.process_time()
        x = 0
        for i in range(150000):
            x += i % 7
        d = {}
        for i in range(30000):
            d[str(i)] = i
        best = min(best, time.process_time() - t)
    return best


# --------------------------------------------------------------------------
if mode == "plain":
    tests = load_tests(sys.argv[3])
    N = len(tests)
    mods = import_everything()
    testrun.guard_spawn()
    cost = [0.0] * N
    ok = [False] * N
    duck = [False] * N
    spawn = [False] * N
    setup_cost = {}
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(1)
    os.dup2(devnull, 1)
    for f, classes in groups(tests):
        try:
            mod = testrun.load_module(f)
        except BaseException as e:
            print("import fail", f, repr(e)[:200], file=sys.stderr)
            continue
        for cname, items in classes:
            cls = getattr(mod, cname, None)
            if cls is None:
                continue
            t0 = time.process_time()
            good = testrun.class_setup(cls)
            setup_cost[f + "::" + cname] = time.process_time() - t0
            for i, m in items:
                if tests[i][3] >= heavy.HEAVY_SECONDS and (cname, m) in heavy.NEVER_RUN:
                    continue
                shim.USED[0] = 0
                testrun.SPAWNED[0] = 0
                t0 = time.process_time()
                ok[i] = good and testrun.run_case(cls, m)
                cost[i] = time.process_time() - t0
                duck[i] = shim.USED[0] > 0
                spawn[i] = testrun.SPAWNED[0] > 0
            testrun.class_teardown(cls)
    gold = heavy.golden(ROOT)
    stand = heavy.check_standins(ROOT, tests)
    os.dup2(saved, 1)
    pickle.dump(
        dict(cost=cost, ok=ok, duck=duck, spawn=spawn, setup_cost=setup_cost, gold=gold,
             stand=stand, bench=bench(), real_duck=REAL_DUCK, mods=mods),
        open(sys.argv[4], "wb"),
    )
    bad = [tests[i][:3] for i in range(N) if not ok[i]]
    print("plain: %d tests, %d not ok, %d duck, %.1fs cpu" % (N, len(bad), sum(duck), sum(cost)),
          file=sys.stderr)
    for b in bad:
        print("  not ok:", "::".join(b), file=sys.stderr)
    sys.exit(0)

# --------------------------------------------------------------------------
if mode == "trace":
    tests = load_tests(sys.argv[3])
    out_dir = sys.argv[4]
    N = len(tests)
    mon = sys.monitoring
    TOOL = 3
    mon.use_tool_id(TOOL, "blast")
    hits = set()

    def on_line(code, line):
        hits.add((code.co_filename, line))
        return mon.DISABLE

    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)
    mods = import_everything()
    import_lines = {}
    for fn, ln in list(hits):
        r = rel(fn)
        if r:
            import_lines.setdefault(r, set()).add(ln)
    hits.clear()

    linemap, openmap, listmap, instmap, reqmap = {}, {}, {}, {}, {}
    casemap, noncase, ncases = {}, {}, {}
    cur_open, cur_list, cur_inst, cur_req = set(), set(), set(), set()
    state = {"idx": (), "case": None}

    def flush():
        bits = 0
        for i in state["idx"]:
            bits |= 1 << i
        if bits:
            case = state["case"] if len(state["idx"]) == 1 else None
            one = state["idx"][0]
            for fn, ln in list(hits):
                r = rel(fn)
                if r:
                    d = linemap.setdefault(r, {})
                    d[ln] = d.get(ln, 0) | bits
                    if case is not None:
                        e = casemap.setdefault(r, {}).setdefault(ln, {}).setdefault(one, {})
                        e[case[0]] = e.get(case[0], 0) | (1 << case[1])
                    elif one < N:
                        nc = noncase.setdefault(r, {})
                        nc[ln] = nc.get(ln, 0) | bits
            for p in cur_open:
                openmap[p] = openmap.get(p, 0) | bits
            for p in cur_list:
                listmap[p] = listmap.get(p, 0) | bits
            for c in cur_inst:
                for k in c.__mro__:
                    if k.__module__.startswith("sqlglot"):
                        key = k.__module__ + ":" + k.__qualname__
                        instmap[key] = instmap.get(key, 0) | bits
            for m in cur_req:
                parts = m.split(".")
                for j in range(1, len(parts) + 1):
                    key = ".".join(parts[:j])
                    reqmap[key] = reqmap.get(key, 0) | bits
        hits.clear()
        cur_open.clear()
        cur_list.clear()
        cur_inst.clear()
        cur_req.clear()

    def switch(idx):
        flush()
        state["idx"] = tuple(idx)
        state["case"] = None
        mon.restart_events()

    def on_case(fname, k):
        flush()
        if fname is None or len(state["idx"]) != 1:
            state["case"] = None
        else:
            state["case"] = (fname, k)
            d = ncases.setdefault(state["idx"][0], {})
            d[fname] = max(d.get(fname, 0), k + 1)
        mon.restart_events()

    testrun.HOOK[0] = on_case

    # ---- hooks -------------------------------------------------------------
    real_open = builtins.open

    def note_path(p, bucket):
        try:
            if isinstance(p, int):
                return
            r = rel(os.path.realpath(os.fspath(p)))
            if r is not None:
                bucket.add(r)
        except Exception:
            pass

    def my_open(file, *a, **k):
        note_path(file, cur_open)
        return real_open(file, *a, **k)

    builtins.open = my_open
    io.open = my_open
    real_open_code = io.open_code

    def my_open_code(path):
        note_path(path, cur_open)
        return real_open_code(path)

    io.open_code = my_open_code
    for name in ("listdir", "scandir"):
        def wrap(fn):
            def inner(path=".", *a, **k):
                note_path(path, cur_list)
                return fn(path, *a, **k)
            return inner
        setattr(os, name, wrap(getattr(os, name)))
    for name in ("stat", "lstat"):
        pass  # existence checks are not recorded

    real_import = builtins.__import__

    def my_import(name, globals=None, locals=None, fromlist=(), level=0):
        try:
            full = name
            if level:
                pkg = (globals or {}).get("__package__") or ""
                base = pkg.rsplit(".", level - 1)[0] if level > 1 else pkg
                full = base + ("." + name if name else "")
            if full.startswith(("sqlglot", "tests")):
                cur_req.add(full)
                for x in fromlist or ():
                    if (full + "." + x) in sys.modules:
                        cur_req.add(full + "." + x)
        except Exception:
            pass
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = my_import
    real_import_module = importlib.import_module

    def my_import_module(name, package=None):
        if name.startswith(("sqlglot", "tests")):
            cur_req.add(name)
        return real_import_module(name, package)

    importlib.import_module = my_import_module

    def wrap_init(cls):
        orig = cls.__dict__["__init__"]

        def __init__(self, *a, **k):
            cur_inst.add(type(self))
            return orig(self, *a, **k)

        __init__.__wrapped__ = orig
        try:
            cls.__init__ = __init__
            return True
        except Exception:
            return False

    tracked = []
    for mn in mods:
        m = sys.modules[mn]
        for v in list(vars(m).values()):
            if isinstance(v, type) and v.__module__ == mn and "__init__" in v.__dict__ \
                    and callable(v.__dict__["__init__"]) and not issubclass(v, BaseException):
                import enum
                if issubclass(v, enum.Enum):
                    continue
                if wrap_init(v):
                    tracked.append(mn + ":" + v.__qualname__)

    # ---- pseudo tests ------------------------------------------------------
    pseudo = {}

    def pid(name):
        if name not in pseudo:
            pseudo[name] = N + len(pseudo)
        return pseudo[name]

    index = {(f, c, m): i for i, (f, c, m, _) in enumerate(tests)}
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(1)
    os.dup2(devnull, 1)
    t_start = time.time()
    for f, classes in groups(tests):
        all_idx = [i for _, items in classes for i, _ in items]
        switch(all_idx)
        try:
            mod = testrun.load_module(f)
        except BaseException as e:
            print("import fail", f, repr(e)[:200], file=sys.stderr)
            continue
        for cname, items in classes:
            cls = getattr(mod, cname, None)
            if cls is None:
                continue
            switch([i for i, _ in items] + [pid("setup:" + f + "::" + cname)])
            testrun.class_setup(cls)
            for i, m in items:
                if (cname, m) in heavy.TPC_TESTS:
                    continue
                switch([i])
                testrun.run_case(cls, m)
            if cname == "TestExecutor":
                heavy.trace_tpc(mod, cls, index, f, pid, switch)
            switch([i for i, _ in items])
            testrun.class_teardown(cls)
        print("traced", f, "%.0fs" % (time.time() - t_start), file=sys.stderr)
    switch(())
    os.dup2(saved, 1)
    mon.set_events(TOOL, 0)
    pickle.dump(
        dict(linemap=linemap, openmap=openmap, listmap=listmap, instmap=instmap, reqmap=reqmap,
             import_lines=import_lines, pseudo=pseudo, tracked=tracked, mods=mods,
             casemap=casemap, noncase=noncase, ncases=ncases,
             ids=["::".join(t[:3]) for t in tests], seconds=[t[3] for t in tests]),
        real_open(os.path.join(out_dir, "trace.pkl"), "wb"),
    )
    print("trace done: %d files, %d pseudo" % (len(linemap), len(pseudo)), file=sys.stderr)
    sys.exit(0)

# --------------------------------------------------------------------------
if mode == "merge":
    out_dir = sys.argv[3]
    tr = pickle.load(open(os.path.join(out_dir, "trace.pkl"), "rb"))
    p1 = pickle.load(open(sys.argv[4], "rb"))
    p2 = pickle.load(open(sys.argv[5], "rb"))
    N = len(tr["ids"])
    linemap = tr["linemap"]

    def dump(name, obj):
        raw = zlib.compress(pickle.dumps(obj, protocol=5), 9)
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(raw)
        return len(raw)

    # identifiers defined at module or class level anywhere in sqlglot
    defined = set()
    trees = {}
    for base in ("sqlglot", "tests"):
        for dp, dn, fns in os.walk(os.path.join(ROOT, base)):
            for fn in fns:
                if fn.endswith(".py"):
                    p = os.path.join(dp, fn)
                    try:
                        trees[rel(p)] = ast.parse(open(p, encoding="utf-8").read())
                    except SyntaxError:
                        pass

    def level_names(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(st.name)
            elif isinstance(st, ast.ClassDef):
                defined.add(st.name)
                level_names(st.body)
            elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in tg:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            defined.add(n.id)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for fld in ("body", "orelse", "finalbody"):
                    level_names(getattr(st, fld, []))
                for h in getattr(st, "handlers", []):
                    level_names(h.body)

    for r, tree in trees.items():
        if r.startswith("sqlglot/"):
            level_names(tree.body)

    ident = {}
    for r, tree in trees.items():
        lm = linemap.get(r)
        if not lm:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                nm = n.id
            elif isinstance(n, ast.Attribute):
                nm = n.attr
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                nm = n.value
            else:
                continue
            if nm not in defined:
                continue
            m = 0
            for ln in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                m |= lm.get(ln, 0)
            if m:
                ident[nm] = ident.get(nm, 0) | m

    # tests are unusable in the runner when they fail at the pinned commit in
    # either plain run, start subprocesses, or behave differently by hash seed
    # harness lines of the TPC tests: an edit there makes the three tests unprovable
    ex = "tests/test_executor.py"
    ps = tr["pseudo"]
    trio = 0
    for i, tid in enumerate(tr["ids"]):
        if tid.startswith(ex) and tuple(tid.split("::")[1:]) in heavy.TPC_TESTS:
            trio |= 1 << i
    trio |= (1 << ps["B:h"]) | (1 << ps["B:ds"])
    for n in ast.walk(trees[ex]):
        if isinstance(n, ast.FunctionDef) and n.name in heavy.HARNESS:
            for ln in range(n.lineno, n.end_lineno + 1):
                linemap[ex][ln] = linemap[ex].get(ln, 0) | trio

    ok = [a and b for a, b in zip(p1["ok"], p2["ok"])]
    cost = [max(a, b) for a, b in zip(p1["cost"], p2["cost"])]
    duck = [a or b for a, b in zip(p1["duck"], p2["duck"])]
    for real in (p1, p2):  # a run with the real duckdb is the truth for tests that use it
        if real["real_duck"]:
            ok = [real["ok"][i] if duck[i] else ok[i] for i in range(N)]
    spawn = [a or b for a, b in zip(p1["spawn"], p2["spawn"])]
    gold = {k: v for k, v in p1["gold"].items() if p2["gold"].get(k, (None,))[0] == v[0]}
    unstable = sorted(set(p1["gold"]) - set(gold))
    print("unstable golden digests:", unstable, file=sys.stderr)
    stand = {k: (p1["stand"][k][0] and p2["stand"][k][0],
                 max(p1["stand"][k][1], p2["stand"][k][1])) for k in p1["stand"]}

    sizes = {}
    covdir = os.path.join(out_dir, "cov")
    os.makedirs(covdir, exist_ok=True)
    for r, lm in linemap.items():
        uniq = {}
        packed = {}
        for ln, m in lm.items():
            packed[ln] = uniq.setdefault(m, len(uniq))
        allm = 0
        for m in uniq:
            allm |= m
        sizes[r] = dump("cov/" + r.replace("/", "__") + ".bin", (list(uniq), packed))
    # per fixture case coverage, for tests expensive enough to be worth filtering
    worth = {i for i in tr["ncases"] if i < N and ok[i] and cost[i] >= 0.1}
    os.makedirs(os.path.join(out_dir, "case"), exist_ok=True)
    csize = 0
    for r, cm in tr["casemap"].items():
        intern = {}
        lines_out = {}
        for ln, per in cm.items():
            keep = {}
            for i, files in per.items():
                if i in worth:
                    key = tuple(sorted(files.items()))
                    keep[i] = intern.setdefault(key, files)
            if keep:
                lines_out[ln] = keep
        if lines_out:
            csize += dump("case/" + r.replace("/", "__") + ".bin", (lines_out, tr["noncase"].get(r, {})))
    print("case", csize, "tests worth filtering", len(worth), file=sys.stderr)
    filemask = {}
    for r, lm in linemap.items():
        m = 0
        for v in lm.values():
            m |= v
        filemask[r] = m
    meta = dict(
        ids=tr["ids"], seconds=tr["seconds"], N=N, pseudo=tr["pseudo"], ok=ok, cost=cost,
        duck=duck, spawn=spawn, setup_cost=p1["setup_cost"], gold=gold, stand=stand,
        bench=min(p1["bench"], p2["bench"]), mods=tr["mods"],
        import_lines={k: sorted(v) for k, v in tr["import_lines"].items()},
        filemask=filemask, real_duck=p1["real_duck"], tracked=set(tr["tracked"]),
        ncases={i: v for i, v in tr["ncases"].items() if i in worth},
        openmap=tr["openmap"], listmap=tr["listmap"], reqmap=tr["reqmap"],
        noisy=pickle.load(open(os.path.join(out_dir, "noisy.pkl"), "rb"))
        if os.path.exists(os.path.join(out_dir, "noisy.pkl")) else set(),
    )
    print("meta", dump("meta.bin", meta), "inst", dump("inst.bin", tr["instmap"]),
          "ident", dump("ident.bin", ident), "cov", sum(sizes.values()), file=sys.stderr)
