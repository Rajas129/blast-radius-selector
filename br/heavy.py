"""The five heavy tests: never run for real, each has a cheap check instead.

test_max_nodes / test_generator_memory: the same test body with smaller loop
constants, served through the overlay.
The three TPC tests: their verdict depends only on (a) the DuckDB SQL sqlglot
renders per query and (b) the optimized tree handed to the executor.  If both
match the pinned commit for every query the change can reach, and the change
reaches nothing that runs after optimization, the test passes.
"""
import hashlib
import os
import pickle
import re
import sys
import time

HEAVY_SECONDS = 2.5
TPC_TESTS = {
    ("TestExecutor", "test_execute_tpch"),
    ("TestExecutor", "test_execute_tpcds"),
    ("TestExecutor", "test_optimized_tpch"),
}
STANDIN = {
    ("TestParser", "test_max_nodes"): "tests/test_parser.py",
    ("TestGenerator", "test_generator_memory"): "tests/test_generator.py",
}
NEVER_RUN = TPC_TESTS | set(STANDIN)
HARNESS = {"test_execute_tpch", "test_execute_tpcds", "test_optimized_tpch", "_mp_execute",
           "subtestHelper", "cached_execute", "rename_anonymous", "mp_execute", "initializer",
           "open_file"}

_SUBS = {
    "tests/test_parser.py": [
        ("        width = 10000\n", "        width = 500\n"),
        ("            parse_one(sql, max_nodes=1_000_000)\n        self.assertIn(\"Maximum",
         "            parse_one(sql, max_nodes=50_000)\n        self.assertIn(\"Maximum"),
    ],
    "tests/test_generator.py": [
        ("                for _ in range(100):\n                    dialect.generator()",
         "                for _ in range(5):\n                    dialect.generator()"),
        ("                    for _ in range(1000):\n                        dialect.generator()",
         "                    for _ in range(40):\n                        dialect.generator()"),
    ],
}


_LOOP = "        for name in Dialects:\n            with self.subTest(dialect=name):\n"


def standin_text(relpath, text, dialects=None):
    """The test file with small constants, or None when it no longer has the expected shape.

    dialects: when the change is confined to some dialects, test_generator_memory
    only needs to loop over those.
    """
    for old, new in _SUBS[relpath]:
        if text.count(old) != 1:
            return None
        text = text.replace(old, new)
    if dialects is not None and text.count(_LOOP) == 1:
        text = text.replace(_LOOP, _LOOP.replace(
            "in Dialects:", "in [d for d in Dialects if d.value in %r]:" % (sorted(dialects),)))
    return text


_DIALECT_MOD = re.compile(r"^sqlglot\.(dialects|parsers|generators|typing)\.(\w+)$")


def dialect_scope(changed_mods, universe):
    """Dialect names a change is confined to, or None when it may touch all of them."""
    from br import state

    if not changed_mods:
        return None
    for m in changed_mods:
        mm = _DIALECT_MOD.match(m)
        if not mm or mm.group(2) == "dialect":
            return None
    deps = state.dependents(set(changed_mods), universe)
    return {d.rsplit(".", 1)[1] for d in deps if d.startswith("sqlglot.dialects.")}


def _md5(s):
    return hashlib.md5(s.encode("utf-8", "replace")).hexdigest()[:16]


def _tree_digest(e):
    return repr(e) + "|" + "|".join(repr(n._type) for n in e.walk() if n._type is not None)


_FIX = {}


def fixtures(suite):
    """[(meta, sql, optimized)] of a suite, read through open() so the overlay applies."""
    if suite not in _FIX:
        from tests.helpers import load_sql_fixture_pairs, TPCH_SCHEMA, TPCDS_SCHEMA

        name = "optimizer/tpc-h/tpc-h.sql" if suite == "h" else "optimizer/tpc-ds/tpc-ds.sql"
        _FIX[suite] = (list(load_sql_fixture_pairs(name)), TPCH_SCHEMA if suite == "h" else TPCDS_SCHEMA)
    return _FIX[suite]


def digest(name):
    """Digest of the artifacts behind pseudo test ``name`` ("A:ds:17", "O:h:3")."""
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    kind, suite, i = name.split(":")
    sqls, schema = fixtures(suite)
    meta, sql, optimized = sqls[int(i)]
    if kind == "O":
        return _md5(transpile(optimized, write="duckdb")[0])
    # the real test ships every parsed query to a pool worker, executed or not
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    out = ""
    if suite == "h" or meta.get("execute"):
        out = transpile(sql, write="duckdb")[0]
    if meta.get("execute"):
        opt = optimize(e, ensure_schema(schema, dialect=None), leave_tables_isolated=True, dialect=None)
        out += "\0" + _tree_digest(opt)
    return _md5(out)


def all_names():
    names = []
    for suite in ("h", "ds"):
        for i in range(len(fixtures(suite)[0])):
            names.append("A:%s:%d" % (suite, i))
            if suite == "h":
                names.append("O:h:%d" % i)
    return names


def golden(root):
    gold = {}
    for name in all_names():
        t = time.process_time()
        d = digest(name)
        gold[name] = (d, time.process_time() - t)
    return gold


def check_standins(root, tests):
    """Do the small-constant tests pass at the pinned commit, and what do they cost?"""
    from br import overlay, testrun

    files = {}
    for rel in _SUBS:
        files[rel] = standin_text(rel, open(os.path.join(root, rel), encoding="utf-8").read())
    if any(v is None for v in files.values()):
        return {k: (False, 0.0) for k in STANDIN}
    overlay.install(root, files)
    out = {}
    for (cname, m), rel in STANDIN.items():
        sys.modules.pop(testrun.modname(rel), None)
        mod = testrun.load_module(rel)
        cls = getattr(mod, cname)
        testrun.class_setup(cls)
        t = time.process_time()
        ok = testrun.run_case(cls, m)
        out[(cname, m)] = (ok, time.process_time() - t)
        testrun.class_teardown(cls)
    return out


def trace_tpc(mod, cls, index, f, pid, switch):
    """Replay the three TPC tests query by query, attributing lines to pseudo tests."""
    import sqlglot.executor as ex
    from sqlglot import find_tables, parse_one, transpile
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA

    e_h = index[(f, "TestExecutor", "test_execute_tpch")]
    e_ds = index[(f, "TestExecutor", "test_execute_tpcds")]
    o_h = index[(f, "TestExecutor", "test_optimized_tpch")]
    real_opt = ex.optimize
    cur = {}

    def wrapped(*a, **k):
        prev = cur["idx"]
        switch(cur["A"])
        try:
            return real_opt(*a, **k)
        finally:
            switch(prev)

    ex.optimize = wrapped
    try:
        switch([e_h, pid("B:h")])
        pickle.dumps((TPCH_SCHEMA, cls.tpch_tables))     # pool initializer arguments
        switch([e_ds, pid("B:ds")])
        pickle.dumps((TPCDS_SCHEMA, cls.tpcds_tables))
        for suite, sqls, schema, tables, ridx in (
            ("h", cls.tpch_sqls, TPCH_SCHEMA, cls.tpch_tables, e_h),
            ("ds", cls.tpcds_sqls, TPCDS_SCHEMA, cls.tpcds_tables, e_ds),
        ):
            for i, (meta, sql, optimized) in enumerate(sqls):
                a = pid("A:%s:%d" % (suite, i))
                A = [ridx, a] + ([o_h] if suite == "h" else [])
                B = [ridx, pid("B:" + suite)]
                cur["A"] = A
                cur["idx"] = A
                switch(A)
                try:
                    e = pickle.loads(pickle.dumps(parse_one(sql)))
                    if suite == "h" or meta.get("execute"):
                        transpile(sql, write="duckdb")
                    if meta.get("execute"):
                        cur["idx"] = B
                        switch(B)
                        tb = {t.name: tables[t.name] for t in find_tables(e)}
                        pickle.loads(pickle.dumps(ex.execute(e, schema=schema, tables=tb)))
                    if suite == "h":
                        switch([o_h, pid("O:h:%d" % i)])
                        transpile(optimized, write="duckdb")
                except Exception as err:  # noqa
                    print("tpc trace error", suite, i, repr(err)[:120], file=sys.stderr)
    finally:
        ex.optimize = real_opt
        switch(())
