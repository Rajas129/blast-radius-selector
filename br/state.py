"""Fingerprint what import leaves behind: module globals and class attributes.

Used for edits that run at import time (class bodies, module level).  The
pinned state and the patched state are both taken in this process tree, so
hash seeds and set order agree.  Named functions are fingerprinted by name
only: edits to their bodies are handled through line coverage instead.
"""
import enum
import functools
import re
import sys
import types

SKIP = {"__doc__", "__firstlineno__", "__static_attributes__", "__module__", "__qualname__",
        "__dict__", "__weakref__", "__annotations__", "__annotate__", "__annotations_cache__",
        "__parameters__", "__orig_bases__", "__abstractmethods__", "_abc_impl",
        "__builtins__", "__cached__", "__file__", "__loader__", "__name__", "__package__",
        "__path__", "__spec__", "__warningregistry__"}
_ATOM = (bool, int, float, str, bytes, complex, type(None), type(Ellipsis))


class FP:
    def __init__(self):
        self.memo = {}

    def code(self, c):
        k = id(c)
        r = self.memo.get(k)
        if r is None:
            consts = ",".join(self.code(x) if isinstance(x, types.CodeType) else repr(x)
                              for x in c.co_consts)
            r = "%s|%s|%s|%s|%d" % (c.co_code.hex(), consts, ",".join(c.co_names),
                                   ",".join(c.co_varnames[:c.co_argcount]), c.co_flags)
            r = str(hash(r))
            self.memo[k] = r
        return r

    def fp(self, v, d=0):
        t = type(v)
        if t in _ATOM:
            return repr(v)
        if d > 40:
            return "<deep>"
        if t is tuple or t is list:
            parts = [self.fp(x, d + 1) for x in v]
            if v and isinstance(v[0], type) and all(isinstance(x, type) for x in v):
                parts.sort()  # tuples of classes are isinstance() arguments
            return "[" + ",".join(parts) + "]"
        if t is set or t is frozenset:
            return "{" + ",".join(sorted([self.fp(x, d + 1) for x in v])) + "}"
        if t is dict:
            parts = [self.fp(k, d + 1) + ":" + self.fp(x, d + 1) for k, x in v.items()]
            for k in v:
                # keyed by classes: usually built from a set, so the order is an accident
                if isinstance(k, type):
                    parts.sort()
                break
            return "D{" + ",".join(parts) + "}"
        if isinstance(v, type):
            return "C<%s.%s>" % (v.__module__, v.__qualname__)
        if t is types.FunctionType:
            q = v.__qualname__
            if "<" not in q:
                return "F<%s.%s>" % (v.__module__, q)
            k = id(v)
            r = self.memo.get(k)
            if r is None:
                cells = ""
                if v.__closure__:
                    parts = []
                    for c in v.__closure__:
                        try:
                            parts.append(self.fp(c.cell_contents, d + 1))
                        except ValueError:
                            parts.append("<empty>")
                    cells = ",".join(parts)
                r = "L<%s|%s|%s|%s>" % (q, self.code(v.__code__), cells,
                                        self.fp(v.__defaults__, d + 1))
                self.memo[k] = r
            return r
        if t is types.ModuleType:
            return "M<%s>" % v.__name__
        if isinstance(v, enum.Enum):
            return "E<%s.%s>" % (t.__qualname__, v.name)
        if t is staticmethod or t is classmethod:
            return "W" + self.fp(v.__func__, d + 1)
        if t is property:
            return "P" + self.fp(v.fget, d + 1) + self.fp(v.fset, d + 1)
        if t is functools.partial:
            return "PA" + self.fp(v.func, d + 1) + self.fp(v.args, d + 1) + self.fp(v.keywords, d + 1)
        if t is types.MethodType:
            return "BM" + self.fp(v.__func__, d + 1) + self.fp(v.__self__, d + 1)
        if t is re.Pattern:
            return "R<%r,%d>" % (v.pattern, v.flags)
        if isinstance(v, dict):
            return t.__name__ + self.fp(dict(v), d + 1)
        if isinstance(v, (tuple, list)):
            return t.__name__ + self.fp(list(v), d + 1)
        if isinstance(v, (set, frozenset)):
            return t.__name__ + self.fp(set(v), d + 1)
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("sqlglot"):
            k = id(v)
            if k in self.memo:
                return self.memo[k]
            self.memo[k] = "<cycle %s>" % t.__qualname__
            try:
                if hasattr(t, "arg_types") and hasattr(v, "args"):
                    r = "X<%r>" % (v,)
                else:
                    items = []
                    dd = getattr(v, "__dict__", None)
                    if dd:
                        items += sorted(dd.items())
                    for kls in t.__mro__:
                        for s in kls.__dict__.get("__slots__", ()) if isinstance(
                                kls.__dict__.get("__slots__", ()), (tuple, list)) else ():
                            if hasattr(v, s):
                                items.append((s, getattr(v, s)))
                    r = "O<%s.%s>{%s}" % (mod, t.__qualname__, ",".join(
                        [k2 + "=" + self.fp(x, d + 1) for k2, x in items]))
            except Exception:
                r = "O<%s.%s>" % (mod, t.__qualname__)
            self.memo[k] = r
            return r
        q = getattr(v, "__qualname__", None) or getattr(v, "__name__", None)
        if q is not None:
            return "B<%s:%s>" % (t.__name__, q)
        return "T<%s.%s>" % (mod, t.__qualname__)


def _table(f, v):
    """Per-entry fingerprints of dict/set valued attributes."""
    t = type(v)
    try:
        if t is dict:
            return {f.fp(k, 1): hash(f.fp(x, 1)) for k, x in v.items()}
        if t is set or t is frozenset:
            return {f.fp(k, 1): 0 for k in v}
    except Exception:
        pass
    return None


def snapshot(modnames):
    """{(module, class qualname, attr): (digest, entries or None, kind)}"""
    f = FP()
    out = {}

    def walk_class(mn, cls):
        q = cls.__qualname__
        out[(mn, q, "<bases>")] = (hash(",".join(f.fp(b) for b in cls.__bases__)), None, "bases", False)
        for name, v in list(vars(cls).items()):
            if name in SKIP:
                continue
            if isinstance(v, type) and v.__module__ == mn and v.__qualname__.startswith(q + "."):
                walk_class(mn, v)
                continue
            one(mn, q, name, v)

    def one(mn, q, name, v):
        try:
            s = f.fp(v)
        except RecursionError:
            s = "<recursion>"
        kind = "value"
        t = type(v)
        if t in (types.FunctionType, staticmethod, classmethod, property, functools.partial) \
                or (callable(v) and not isinstance(v, type)):
            kind = "callable"
        foreign = False
        if not q:
            foreign = t is types.ModuleType or getattr(v, "__module__", mn) != mn
        out[(mn, q, name)] = (hash(s), _table(f, v), kind, foreign)

    for mn in modnames:
        m = sys.modules.get(mn)
        if m is None:
            continue
        for name, v in list(vars(m).items()):
            if name in SKIP:
                continue
            if isinstance(v, type) and v.__module__ == mn and "." not in v.__qualname__:
                walk_class(mn, v)
            else:
                one(mn, "", name, v)
    return out


def diff(base, now, noisy=()):
    """[(module, class, attr, kind, changed entry keys or None)]"""
    changes = []
    for key in base.keys() | now.keys():
        if key in noisy:
            continue
        a, b = base.get(key), now.get(key)
        if a is not None and b is not None and a[0] == b[0]:
            continue
        kind = (b or a)[2]
        if a is None or b is None:
            if (b or a)[3]:
                if a is None:
                    continue      # a new import: nothing existing can depend on it
                kind = "gone"     # a removed import: breaks code of that module using it
            elif kind != "bases":
                kind = "callable" if kind == "callable" else "newvalue"
            changes.append(key + (kind, None))
            continue
        entries = None
        if a[1] is not None and b[1] is not None:
            entries = [k for k in a[1].keys() | b[1].keys() if a[1].get(k, -1) != b[1].get(k, -1)]
        if a[2] == "callable" and b[2] == "callable":
            kind = "callable"
        elif a[2] != b[2]:
            kind = "value"
        changes.append(key + (kind, entries))
    return changes


def dependents(changed, universe):
    """Modules whose namespace reaches a changed module (transitively)."""
    deps = {}
    for mn in universe:
        m = sys.modules.get(mn)
        if m is None:
            continue
        ds = set()
        for v in vars(m).values():
            if type(v) is types.ModuleType:
                ds.add(v.__name__)
            else:
                om = getattr(v, "__module__", None)
                if type(om) is str:
                    ds.add(om)
        deps[mn] = ds
    out = set(changed)
    grew = True
    while grew:
        grew = False
        for mn, ds in deps.items():
            if mn not in out and ds & out:
                out.add(mn)
                grew = True
    # packages re-export their submodules
    for mn in list(out):
        parts = mn.split(".")
        for j in range(1, len(parts)):
            out.add(".".join(parts[:j]))
    return [mn for mn in universe if mn in out]
