"""Build maps/rel.bin: per test, how comparisons related and which table keys were read.

    python tools/build_rel.py run   <sqlglot_root> <tests.json> <out.pkl> [shard] [nshards]
    python tools/build_rel.py merge <tests.json> <out rel.bin> <shard.pkl>...

Comparisons: every single-operator comparison in sqlglot (is, is not, ==, !=,
<, <=, >, >=) is compiled into a call that notes, per test and per site, whether
the two operands were ever equal-but-not-identical, equal, less or greater.
A patch that only swaps one such operator (say `is` for `==`) can then change
the behaviour of a test only if the test saw the relation where the two
operators disagree.

Tables: dicts and sets held by sqlglot modules and classes are replaced by
subclasses that note which keys a test looked up (and whether it iterated).
A patch that changes some entries of a table can only be noticed by a test that
looked up one of those entries or walked the table.

Tests that cannot be traced this way (the heavy ones, tests that start
processes, tests that fail under the recorder) are marked untraced; the
selector never narrows those.  Everything here comes from the pinned commit.
"""
import ast
import builtins
import gc
import importlib
import importlib.machinery
import json
import os
import pickle
import pkgutil
import sys
import time
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

EXEC, ISNEQ, EQ, LT, GT, ERR = 1, 2, 4, 8, 16, 32
OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}
FULL_ID = EXEC | ISNEQ | EQ
FULL_ORD = EXEC | EQ | LT | GT
SKIP_TESTS = set(filter(None, os.environ.get(
    "BR_REL_SKIP", "test_max_nodes,test_generator_memory,test_execute_tpch,"
    "test_execute_tpcds,test_optimized_tpch").split(",")))
ONLY = set(filter(None, os.environ.get("BR_REL_ONLY", "").split(",")))

S = {"cmp": {}, "keys": set(), "on": False}
SITES = []


# --------------------------------------------------------------------------- comparisons

def _cmp(i, op, a, b):
    if op == 0:
        r = a is b
    elif op == 1:
        r = a is not b
    elif op == 2:
        r = a == b
    elif op == 3:
        r = a != b
    elif op == 4:
        r = a < b
    elif op == 5:
        r = a <= b
    elif op == 6:
        r = a > b
    else:
        r = a >= b
    if not S["on"]:
        return r
    d = S["cmp"]
    f = d.get(i, 0)
    full = FULL_ID if op < 4 else FULL_ORD
    if f & full != full:
        was = S["on"]
        S["on"] = False   # comparisons made while probing are not the test's
        f |= EXEC
        try:
            if op < 4:
                eq = bool(a == b)
                if (a is b) != eq:
                    f |= ISNEQ
                if eq:
                    f |= EQ
            else:
                if bool(a == b):
                    f |= EQ
                if bool(a < b):
                    f |= LT
                if bool(a > b):
                    f |= GT
        except Exception:
            f |= ERR
        S["on"] = was
        d[i] = f
    return r


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        i = len(SITES)
        SITES.append("%s:%d:%d" % (self.rel, node.lineno, node.col_offset))
        call = ast.Call(func=ast.Name(id="__br_cmp__", ctx=ast.Load()),
                        args=[ast.Constant(i), ast.Constant(OPS[type(node.ops[0])]),
                              node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    root = ""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        tree = ast.parse(self.get_data(path), filename=path)
        tree = _Rewrite(os.path.relpath(path, self.root)).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder:
    def __init__(self, root):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not (spec.origin or "").endswith(".py"):
            return spec
        loader = _Loader(name, spec.origin)
        loader.root = self.root
        spec.loader = loader
        return spec


# --------------------------------------------------------------------------- tables

_FP = [None]


def _key(k):
    try:
        return _FP[0]().fp(k, 1)   # fresh fingerprinter: its memo is keyed by id()
    except Exception:
        return "*"


def _rec(tags, k):
    if S["on"]:
        kk = _key(k)
        for t in tags:
            S["keys"].add((t, kk))


def _walk(tags):
    if S["on"]:
        for t in tags:
            S["keys"].add((t, "*"))


def _rec_len(tags):
    # only the number of entries was observed: matters when entries are added or removed
    if S["on"]:
        for t in tags:
            S["keys"].add((t, "#len"))


class RecDict(dict):
    __slots__ = ("_br_tags",)

    def __getitem__(self, k):
        _rec(self._br_tags, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _rec(self._br_tags, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _rec(self._br_tags, k)
        return dict.__contains__(self, k)

    def setdefault(self, k, default=None):
        _rec(self._br_tags, k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *a):
        _rec(self._br_tags, k)
        return dict.pop(self, k, *a)

    def __iter__(self):
        _walk(self._br_tags)
        return dict.__iter__(self)

    def keys(self):
        _walk(self._br_tags)
        return dict.keys(self)

    def items(self):
        _walk(self._br_tags)
        return dict.items(self)

    def values(self):
        _walk(self._br_tags)
        return dict.values(self)

    def copy(self):
        _walk(self._br_tags)
        return dict(dict.items(self))

    def __or__(self, other):
        _walk(self._br_tags)
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        _walk(self._br_tags)
        return other | dict(dict.items(self))

    def __eq__(self, other):
        _walk(self._br_tags)
        return dict.__eq__(self, other)

    __hash__ = None

    def __len__(self):
        _rec_len(self._br_tags)
        return dict.__len__(self)

    def __reduce__(self):
        return (dict, (dict(dict.items(self)),))


def _set_cls(base):
    class RecSet(base):
        __slots__ = ("_br_tags",)

        def __contains__(self, k):
            _rec(self._br_tags, k)
            return base.__contains__(self, k)

        def __iter__(self):
            _walk(self._br_tags)
            return base.__iter__(self)

        def __len__(self):
            _rec_len(self._br_tags)
            return base.__len__(self)

        def __eq__(self, other):
            _walk(self._br_tags)
            return base.__eq__(self, other)

        __hash__ = base.__hash__ if base is frozenset else None

        def __reduce__(self):
            return (base, (list(base.__iter__(self)),))

    for name in ("__or__", "__and__", "__sub__", "__xor__", "__ror__", "__rand__", "__rsub__",
                 "__rxor__", "union", "intersection", "difference", "symmetric_difference",
                 "issubset", "issuperset", "isdisjoint", "copy"):
        if hasattr(base, name):
            def make(n):
                def m(self, *a):
                    _walk(self._br_tags)
                    return getattr(base, n)(base(base.__iter__(self)), *a)
                return m
            setattr(RecSet, name, make(name))
    return RecSet


RecSet = _set_cls(set)
RecFrozenSet = _set_cls(frozenset)
WRAP = {dict: RecDict, set: RecSet, frozenset: RecFrozenSet}


def wrap_tables(modnames, skip):
    """Replace module/class-level dicts and sets by recording ones.

    Returns (tags wrapped, tags whose original object is still referenced elsewhere)."""
    memo = {}       # id(original) -> wrapper
    orig = {}       # id(original) -> original (kept alive)
    tags_of = {}    # id(original) -> [tag]

    def wrap(v, tag):
        k = id(v)
        w = memo.get(k)
        if w is None:
            w = WRAP[type(v)](v)
            w._br_tags = []
            memo[k] = w
            orig[k] = v
            tags_of[k] = w._br_tags
        w._br_tags.append(tag)
        return w

    def walk_class(mn, cls):
        q = cls.__qualname__
        for name, v in list(vars(cls).items()):
            if name in skip:
                continue
            if isinstance(v, type) and v.__module__ == mn and v.__qualname__.startswith(q + "."):
                walk_class(mn, v)
                continue
            if type(v) in WRAP:
                try:
                    setattr(cls, name, wrap(v, (mn, q, name)))
                except Exception:
                    pass

    for mn in modnames:
        m = sys.modules.get(mn)
        if m is None:
            continue
        for name, v in list(vars(m).items()):
            if name in skip:
                continue
            if isinstance(v, type) and v.__module__ == mn and "." not in v.__qualname__:
                walk_class(mn, v)
            elif type(v) in WRAP:
                setattr(m, name, wrap(v, (mn, "", name)))

    # Any other live reference to an original means reads through it go unseen:
    # such tables are "leaky" and never used for narrowing.
    leaky = set()
    holders = {id(memo), id(orig), id(tags_of)}
    ids = set(orig)
    for o in gc.get_objects():
        if id(o) in holders or id(o) in ids:
            continue
        for r in gc.get_referents(o):
            if id(r) in ids:
                leaky.update(tags_of[id(r)])
    wrapped = {t for ts in tags_of.values() for t in ts}
    return wrapped, leaky, orig


# --------------------------------------------------------------------------- run

def run(root, tests_json, out, shard=0, nshards=1):
    root = os.path.realpath(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    builtins.__br_cmp__ = _cmp
    sys.meta_path.insert(0, _Finder(root))
    from br import state, testrun

    _FP[0] = state.FP
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    mods = sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot."))
    wrapped, leaky, keepalive = wrap_tables(mods, state.SKIP)
    print("shard %d: %d tables wrapped, %d leaky, %d comparison sites" % (
        shard, len(wrapped), len(leaky), len(SITES)), file=sys.stderr)
    testrun.guard_spawn()

    tests = [t["id"] for t in json.load(open(tests_json))["tests"]]
    files = {}
    for tid in tests:
        f, c, m = tid.split("::")
        files.setdefault(f, {}).setdefault(c, []).append((tid, m))
    flist = sorted(files)
    mine = [f for j, f in enumerate(flist) if j % nshards == shard]
    if ONLY:
        mine = [f for f in flist if any(m in ONLY for c in files[f].values() for _, m in c)]

    res = {}
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(1)
    os.dup2(devnull, 1)
    t0 = time.time()
    def begin():
        S["cmp"], S["keys"] = {}, set()
        S["on"] = True

    def end():
        S["on"] = False
        return dict(S["cmp"]), set(S["keys"])

    def union(a, b):
        c = dict(a[0])
        for k, v in b[0].items():
            c[k] = c.get(k, 0) | v
        return c, a[1] | b[1]

    for f in mine:
        begin()
        try:
            mod = testrun.load_module(f)
        except BaseException:
            end()
            continue
        file_obs = end()
        for cname, items in files[f].items():
            cls = getattr(mod, cname, None)
            if cls is None:
                continue
            begin()
            try:
                testrun.arm(300)
                okc = testrun.class_setup(cls)
            except BaseException:
                okc = False
            finally:
                testrun.disarm()
            cls_obs = union(file_obs, end())
            if not okc:
                continue
            for tid, m in items:
                if m in SKIP_TESTS or (ONLY and m not in ONLY):
                    continue
                testrun.SPAWNED[0] = 0
                begin()
                ok = False
                try:
                    testrun.arm(240)
                    ok = testrun.run_case(cls, m)
                except BaseException:
                    ok = False
                finally:
                    testrun.disarm()
                    obs = union(cls_obs, end())
                if ok and not testrun.SPAWNED[0]:
                    res[tid] = ({SITES[i]: fl for i, fl in obs[0].items()}, obs[1])
            testrun.class_teardown(cls)
        print("shard %d: %s done, %.0fs" % (shard, f, time.time() - t0), file=sys.stderr)
    os.dup2(saved, 1)
    with open(out, "wb") as fh:
        pickle.dump(dict(res=res, wrapped=wrapped, leaky=leaky), fh, protocol=5)
    print("shard %d: %d tests traced" % (shard, len(res)), file=sys.stderr)


def merge(tests_json, out, parts):
    tests = [t["id"] for t in json.load(open(tests_json))["tests"]]
    num = {tid: i for i, tid in enumerate(tests)}
    covered = 0
    cmp = {}
    keys = {}
    wrapped, leaky = set(), set()
    for p in parts:
        d = pickle.load(open(p, "rb"))
        wrapped |= d["wrapped"]
        leaky |= d["leaky"]
        for tid, (c, ks) in d["res"].items():
            bit = 1 << num[tid]
            covered |= bit
            for site, fl in c.items():
                e = cmp.setdefault(site, {})
                b = 1
                while b <= fl:
                    if fl & b:
                        e[b] = e.get(b, 0) | bit
                    b <<= 1
            for tag, k in ks:
                e = keys.setdefault(tag, {})
                e[k] = e.get(k, 0) | bit
    obj = dict(ids=tests, covered=covered, cmp=cmp, keys=keys, wrapped=wrapped - leaky, leaky=leaky)
    raw = zlib.compress(pickle.dumps(obj, protocol=5), 9)
    with open(out, "wb") as fh:
        fh.write(raw)
    print("rel.bin: %d bytes, %d traced tests, %d sites, %d tables (%d leaky)" % (
        len(raw), bin(covered).count("1"), len(cmp), len(wrapped - leaky), len(leaky)), file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "run":
        a = sys.argv[2:]
        run(a[0], a[1], a[2], int(a[3]) if len(a) > 3 else 0, int(a[4]) if len(a) > 4 else 1)
    else:
        merge(sys.argv[2], sys.argv[3], sys.argv[4:])
