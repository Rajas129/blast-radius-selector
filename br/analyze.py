"""Static analysis of one patch against the maps of the pinned commit."""
import os
import pickle
import zlib

from br import patch as P

CONFIG_ALWAYS = {"conftest.py", "pytest.ini", "tox.ini", ".pytest.ini"}
CONFIG_MAYBE = {"setup.cfg", "pyproject.toml"}
SPECIAL_TEST_NAMES = {"setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule",
                      "tearDownModule", "run", "debug", "id"}


class Maps:
    def __init__(self, d, root):
        self.dir, self.root = d, root
        self.meta = self._load("meta.bin")
        self.N = self.meta["N"]
        self.REAL = (1 << self.N) - 1
        self.pseudo = self.meta["pseudo"]
        self._cov = {}
        self._inst = self._ident = None
        self._case = {}
        self.index = {tid: i for i, tid in enumerate(self.meta["ids"])}
        self.file_tests = {}
        for i, tid in enumerate(self.meta["ids"]):
            f = tid.split("::", 1)[0]
            self.file_tests[f] = self.file_tests.get(f, 0) | (1 << i)

    def _load(self, name):
        with open(os.path.join(self.dir, name), "rb") as fh:
            return pickle.loads(zlib.decompress(fh.read()))

    def cov(self, rel):
        c = self._cov.get(rel)
        if c is None:
            try:
                c = self._load("cov/" + rel.replace("/", "__") + ".bin")
            except OSError:
                c = ([], {})
            self._cov[rel] = c
        return c

    def case(self, rel):
        c = self._case.get(rel)
        if c is None:
            try:
                c = self._load("case/" + rel.replace("/", "__") + ".bin")
            except OSError:
                c = ({}, {})
            self._case[rel] = c
        return c

    def lines(self, rel, lines):
        uniq, packed = self.cov(rel)
        m = 0
        seen = set()
        for ln in lines:
            k = packed.get(ln)
            if k is not None and k not in seen:
                seen.add(k)
                m |= uniq[k]
        return m

    @property
    def inst(self):
        if self._inst is None:
            self._inst = self._load("inst.bin")
        return self._inst

    @property
    def ident(self):
        if self._ident is None:
            self._ident = self._load("ident.bin")
        return self._ident

    def bit(self, name):
        i = self.pseudo.get(name)
        return 0 if i is None else 1 << i

    def old_text(self, rel):
        try:
            with open(os.path.join(self.root, rel), encoding="utf-8", errors="surrogateescape",
                      newline="") as fh:
                return fh.read()
        except (OSError, ValueError):
            return None


# --------------------------------------------------------------------------- AST diff
class Diff:
    def __init__(self):
        self.lines = set()   # old lines whose coverage picks the tests
        self.imp = False     # something that runs at import time changed
        self.funcs = []      # (old def, new def) pairs with a changed body
        self.syntax = False
        self.same = False
        self.new_defs = []   # names of defs added at class/module level
        self.only_new_defs = True


class _Ctx:
    """Statements the patch does not touch are equal by construction: give them a
    position token instead of dumping them (ast.dump of a big module is slow)."""

    def __init__(self, edits):
        self.edits = edits
        self.new_side = False

    def touched(self, first, last):
        for o0, o1, n0, n1 in self.edits:
            a, b = (n0, n1) if self.new_side else (o0, o1)
            if a == b:          # nothing on this side: an insertion point
                if first < a <= last:
                    return True
            elif a <= last and first < b:
                return True
        return False

    def old_line(self, n):
        shift = 0
        for o0, o1, n0, n1 in self.edits:
            if n1 <= n:
                shift += (n1 - n0) - (o1 - o0)
        return n - shift


_CTX = [None]
_IS_NONE = "ops=[Is()], comparators=[Constant(value=None)]"
_ISNOT_NONE = "ops=[IsNot()], comparators=[Constant(value=None)]"


def _dump_old(st):
    return _dump(st, False)


def _dump_new(st):
    return _dump(st, True)


def _dump(st, new_side):
    import ast
    ctx = _CTX[0]
    first = _first(st) if hasattr(st, "lineno") else None
    if ctx is not None and first is not None:
        ctx.new_side = new_side
        if not ctx.touched(first, st.end_lineno or first):
            return ("u", ctx.old_line(first) if new_side else first)
    d = ast.dump(st)
    # "x is None" and "x == None" only differ for objects whose __eq__ claims to equal None
    if "Constant(value=None)" in d:
        d = d.replace(_IS_NONE, "ops=[Eq()], comparators=[Constant(value=None)]").replace(
            _ISNOT_NONE, "ops=[NotEq()], comparators=[Constant(value=None)]")
    return d


def _strip_doc(body):
    import ast
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _first(st):
    decs = getattr(st, "decorator_list", None)
    return min([st.lineno] + [d.lineno for d in decs]) if decs else st.lineno


def _span(st):
    return range(_first(st), (st.end_lineno or st.lineno) + 1)


def _diff_block(ob, nb, parent, R):
    import ast
    import difflib
    D, E = _dump_old, _dump_new
    ob, nb = _strip_doc(ob), _strip_doc(nb)
    od, nd = [D(s) for s in ob], [E(s) for s in nb]
    if od == nd:
        return
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, od, nd, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and _diff_stmt(ob[i1], nb[j1], R):
            continue
        for s in ob[i1:i2]:
            R.lines.update(_span(s))
        if j2 > j1 and i2 == i1:
            anchor = ob[i1 - 1] if i1 > 0 else (ob[i1] if i1 < len(ob) else parent)
            R.lines.update(_span(anchor))


def _diff_stmt(a, b, R):
    """Recurse into a compound statement whose shape is unchanged. False: treat as replaced."""
    import ast
    D, E = _dump_old, _dump_new
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.If):
        if D(a.test) != E(b.test):
            R.lines.update(range(a.test.lineno, (a.test.end_lineno or a.test.lineno) + 1))
            R.lines.add(a.lineno)
        _diff_block(a.body, b.body, a, R)
        if bool(a.orelse) != bool(b.orelse):
            R.lines.update(_span(a))
        else:
            _diff_block(a.orelse, b.orelse, a, R)
        return True
    if isinstance(a, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        if isinstance(a, ast.While):
            same = D(a.test) == E(b.test)
        elif isinstance(a, (ast.With, ast.AsyncWith)):
            same = [D(x) for x in a.items] == [E(x) for x in b.items]
        else:
            same = D(a.target) == E(b.target) and D(a.iter) == E(b.iter)
        if not same:
            R.lines.update(range(a.lineno, a.body[0].lineno + 1))
        _diff_block(a.body, b.body, a, R)
        if bool(getattr(a, "orelse", None)) != bool(getattr(b, "orelse", None)):
            R.lines.update(_span(a))
        elif getattr(a, "orelse", None):
            _diff_block(a.orelse, b.orelse, a, R)
        return True
    if isinstance(a, ast.Try):
        if len(a.handlers) != len(b.handlers) or bool(a.orelse) != bool(b.orelse) \
                or bool(a.finalbody) != bool(b.finalbody):
            return False
        for h1, h2 in zip(a.handlers, b.handlers):
            if D(h1.type) != E(h2.type) if (h1.type and h2.type) else (h1.type is not h2.type):
                return False
            if h1.name != h2.name:
                return False
        _diff_block(a.body, b.body, a, R)
        for h1, h2 in zip(a.handlers, b.handlers):
            _diff_block(h1.body, h2.body, a, R)
        _diff_block(a.orelse, b.orelse, a, R)
        _diff_block(a.finalbody, b.finalbody, a, R)
        return True
    return False


def _diff_scope(ob, nb, R):
    import ast
    D, E = _dump_old, _dump_new
    ob, nb = _strip_doc(ob), _strip_doc(nb)
    DEF = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    if [D(s) for s in ob if not isinstance(s, DEF)] != [E(s) for s in nb if not isinstance(s, DEF)]:
        R.imp = True
        R.only_new_defs = False
    od, nd = {}, {}
    for body, tab in ((ob, od), (nb, nd)):
        for s in body:
            if isinstance(s, DEF):
                tab.setdefault((type(s), s.name), []).append(s)
    for key in od.keys() | nd.keys():
        o, n = od.get(key, []), nd.get(key, [])
        if len(o) != len(n):
            R.imp = True
            if len(o) == 0 and key[0] is not ast.ClassDef:
                R.new_defs.append(key[1])
            else:
                R.only_new_defs = False
                for s in o:
                    R.lines.update(_span(s))
            continue
        for a, b in zip(o, n):
            if D(a) == E(b):
                continue
            if isinstance(a, ast.ClassDef):
                if [D(x) for x in a.bases + a.keywords + a.decorator_list] != \
                        [E(x) for x in b.bases + b.keywords + b.decorator_list]:
                    R.imp = True
                    R.only_new_defs = False
                _diff_scope(a.body, b.body, R)
            else:
                ha = ast.dump(a.args) + "|".join(ast.dump(x) for x in a.decorator_list)
                hb = ast.dump(b.args) + "|".join(ast.dump(x) for x in b.decorator_list)
                if ha != hb:
                    R.imp = True
                    R.only_new_defs = False
                    R.lines.update(_span(a))
                if [D(s) for s in _strip_doc(a.body)] != [E(s) for s in _strip_doc(b.body)]:
                    R.funcs.append((a, b))
                    _diff_block(a.body, b.body, a, R)


_AST = {}


def diff_python(old, new, edits=None, key=None):
    import ast
    R = Diff()
    try:
        ta = _AST.get(key) if key else None
        if ta is None:
            ta = ast.parse(old)
            if key:
                _AST[key] = ta
    except (SyntaxError, ValueError, RecursionError):
        R.syntax = R.imp = True
        return R
    try:
        tb = ast.parse(new)
    except (SyntaxError, ValueError, RecursionError):
        R.syntax = R.imp = True
        return R
    _CTX[0] = _Ctx(edits) if edits else None
    try:
        _diff_scope(ta.body, tb.body, R)
    finally:
        _CTX[0] = None
    R.same = not (R.imp or R.lines or R.funcs)
    return R


# --------------------------------------------------------------------------- plan
class Plan:
    def __init__(self):
        self.all = False
        self.mask = 0      # candidates that can be checked by running
        self.keep = 0      # selected without running
        self.files = {}    # overlay: rel -> text / None
        self.reimport = False
        self.state = False
        self.swaps = []    # (abs file, old first line, name, new first line, rel)
        self.changed_mods = []
        self.rt = {}       # rel -> old lines whose coverage produced candidates
        self.other = 0     # candidates that did not come from line coverage
        self.wide = 0      # everything that runs the patched files: free idle-time insurance
        self.hint = set()  # words from patched paths, to order the run
        self.test_edit = set()


def _mod_of(rel):
    name = rel[:-3].replace("/", ".")
    return name[:-9] if name.endswith(".__init__") else name


def _listers(maps, rel):
    lm = maps.meta["listmap"]
    d = os.path.dirname(rel)
    while d and not os.path.isdir(os.path.join(maps.root, d)):
        d = os.path.dirname(d)
    return lm.get(d, 0) | (lm.get(os.path.dirname(rel), 0))


def plan_commit(maps, text):
    meta = maps.meta
    pl = Plan()
    files = P.parse(text)
    for fp in files:
        rels = [r for r in (fp.old, fp.new) if r]
        if not rels:
            continue
        base = os.path.basename(rels[-1])
        for r in rels:
            pl.hint.update(w for w in os.path.basename(r).replace(".", "_").split("_") if len(w) > 2)
        if base in CONFIG_ALWAYS or any(os.path.basename(r) in CONFIG_ALWAYS for r in rels):
            pl.all = True
            return pl
        structural = fp.added or fp.deleted or fp.binary or fp.renamed or (
            fp.old and fp.new and fp.old != fp.new)
        if structural and not fp.hunks and not fp.binary and fp.old == fp.new:
            continue  # mode change only
        old_rel = None if fp.added and not fp.renamed else fp.old
        new_rel = None if fp.deleted else (fp.new or fp.old)
        old = maps.old_text(old_rel) if old_rel else None
        new = None
        if new_rel and not fp.binary:
            try:
                new, edits = P.apply(old or "", fp.hunks) if (fp.hunks or old is None) else (old, [])
            except (ValueError, IndexError):
                new = None
                structural = True
                fp.binary = True  # content unknown
        if base in CONFIG_MAYBE:
            blob = (old or "") + (new or "")
            if "[tool:pytest]" in blob or "[pytest]" in blob or "[tool.pytest" in blob:
                pl.all = True
                return pl
        if structural:
            for r in rels:
                pl.keep |= meta["openmap"].get(r, 0) | _listers(maps, r)
                if r.endswith(".py"):
                    pl.keep |= meta["filemask"].get(r, 0)
            if old_rel and old_rel != new_rel:
                pl.files[old_rel] = None
            if new_rel:
                if new is None:
                    # unknown content: everything that touches the file
                    for r in rels:
                        if r.endswith(".py"):
                            pl.keep |= meta["reqmap"].get(_mod_of(r), 0)
                            if r.startswith("sqlglot/") and _mod_of(r) in meta["mods"] \
                                    and not meta["filemask"].get(r):
                                pl.all = True
                    continue
                pl.files[new_rel] = new
            for r in rels:
                if r.endswith(".py") and r.startswith(("sqlglot/", "tests/")):
                    if r.startswith("sqlglot/"):
                        pl.reimport = pl.state = True
                        pl.changed_mods.append(_mod_of(r))
                    else:
                        pl.keep |= meta["reqmap"].get(_mod_of(r), 0)
            continue
        # ---- plain modification
        rel = new_rel
        if old is None or new is None or new == old:
            if old is None and new is not None:
                pl.files[rel] = new
            continue
        pl.files[rel] = new
        readers = meta["openmap"].get(rel, 0)
        if not rel.endswith(".py"):
            pl.keep |= readers
            continue
        pl.mask |= readers
        pl.other |= readers
        R = diff_python(old, new, edits, rel)
        if R.same:
            continue
        mod = _mod_of(rel)
        in_pkg = rel.startswith("sqlglot/")
        in_tests = rel.startswith("tests/")
        if R.syntax:
            if in_pkg:
                pl.reimport = True
                pl.changed_mods.append(mod)
            pl.mask |= meta["reqmap"].get(mod, 0)
            pl.other |= meta["reqmap"].get(mod, 0)
            continue
        pl.mask |= maps.lines(rel, R.lines)
        if R.lines:
            pl.rt[rel] = R.lines
        if in_tests:
            pl.test_edit.add(rel)
            if R.imp:
                names = set(R.new_defs)
                harmless = R.only_new_defs and all(
                    n.startswith("test") and n not in SPECIAL_TEST_NAMES for n in names)
                if not harmless:
                    extra = meta["reqmap"].get(mod, 0) | maps.file_tests.get(rel, 0)
                    pl.mask |= extra
                    pl.other |= extra
            continue
        if not in_pkg:
            if R.imp or R.funcs:
                pl.keep |= meta["reqmap"].get(mod, 0)
            continue
        pl.changed_mods.append(mod)
        pl.wide |= meta["filemask"].get(rel, 0)
        imp_lines = set(meta["import_lines"].get(rel, ()))
        swaps = []
        for a, b in R.funcs:
            body = range(a.body[0].lineno, (a.end_lineno or a.lineno) + 1)
            if any(ln in imp_lines for ln in body):
                R.imp = True  # the function runs while sqlglot is imported
            swaps.append((os.path.join(maps.root, rel), _first(a), a.name, _first(b), rel))
        if R.imp:
            pl.reimport = pl.state = True
        else:
            pl.swaps.extend(swaps)
    if pl.reimport:
        pl.swaps = []
    return pl


# --------------------------------------------------------------------------- state users
def users(maps, changes, info):
    """Tests that can notice the state changes reported by the child.

    info[(module, class)] = (inst key or None, {attr: [inst keys of expressions keyed by attr]})
    """
    inst, ident = maps.inst, maps.ident
    ALL = (1 << (maps.N + len(maps.pseudo))) - 1
    out = 0
    for mn, q, attr, kind, entries in changes:
        base = ALL
        ikey, sqlkeys = info.get((mn, q), (None, {}))
        if q and ikey is not None:
            base &= inst.get(ikey, 0)
        if kind == "gone":
            rel = mn.replace(".", "/")
            fm = maps.meta["filemask"]
            if rel + ".py" in fm:      # a plain module: only its own code and direct importers
                base &= ident.get(attr, 0) & (fm[rel + ".py"] | maps.meta["reqmap"].get(mn, 0))
            else:
                base &= ident.get(attr, 0)
        elif kind == "bases":
            if not q or ikey is None:
                base &= ident.get(q.split(".")[-1], 0) if q else 0
        elif attr.startswith("__") and attr.endswith("__"):
            if ikey is None:
                base &= ident.get(attr, 0)
        elif kind == "callable":
            m = ident.get(attr, 0)
            for k in sqlkeys.get(attr, ()):
                m |= inst.get(k, 0)
            base &= m
        else:
            base &= ident.get(attr, 0)
            if entries and all(e.startswith("C<sqlglot.") for e in entries):
                m = 0
                good = True
                for e in entries:
                    path = e[2:-1]
                    hit = None
                    parts = path.split(".")
                    for j in range(len(parts) - 1, 0, -1):
                        k = ".".join(parts[:j]) + ":" + ".".join(parts[j:])
                        if k in inst:
                            hit = k
                            break
                    if hit is None:
                        if path.startswith("sqlglot.expressions."):
                            continue  # an expression class no test ever builds
                        good = False
                        break
                    m |= inst[hit]
                if good:
                    base &= m
        out |= base
    return out
