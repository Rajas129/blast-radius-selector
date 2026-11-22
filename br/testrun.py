"""Load and run single unittest cases in-process (no pytest)."""
import importlib
import pickle
import signal
import subprocess
import sys
import unittest
from concurrent.futures import Future


class Timeout(BaseException):
    pass


SPAWNED = [0]


class InlinePool:
    """Runs submitted work in this process so patched code is what gets tested."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *a, **k):
        f = Future()
        try:
            # a real pool pickles the arguments and the result; sqlglot code runs in both
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except Timeout:
            raise
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_popen_init = subprocess.Popen.__init__


def _popen_init(self, *a, **k):
    SPAWNED[0] += 1
    return _real_popen_init(self, *a, **k)


def guard_spawn():
    subprocess.Popen.__init__ = _popen_init


def modname(path):
    return path[:-3].replace("/", ".")


CASE = [None]   # {fixture file: bitmask of cases to keep} while a filtered test runs
HOOK = [None]   # tracer callback(fixture file or None, case number)


def _wrap_fixture(fn):
    def inner(filename):
        flt, hook = CASE[0], HOOK[0]
        if flt is None and hook is None:
            yield from fn(filename)
            return
        mask = -1 if flt is None else flt.get(filename, 0)
        k = 0
        for item in fn(filename):
            if (mask >> k) & 1:
                if hook is not None:
                    hook(filename, k)
                yield item
            k += 1
        if hook is not None:
            hook(None, 0)

    inner.__br_wrapped__ = True
    return inner


def load_module(path):
    mod = importlib.import_module(modname(path))
    if hasattr(mod, "ProcessPoolExecutor"):
        mod.ProcessPoolExecutor = InlinePool
    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        fn = getattr(mod, name, None)
        if fn is not None and not getattr(fn, "__br_wrapped__", False):
            setattr(mod, name, _wrap_fixture(fn))
    return mod


def _on_prof(signum, frame):
    raise Timeout()


def arm(seconds):
    signal.signal(signal.SIGPROF, _on_prof)
    signal.setitimer(signal.ITIMER_PROF, max(seconds, 0.05), 0.25)


def disarm():
    signal.setitimer(signal.ITIMER_PROF, 0)


def class_setup(cls):
    try:
        cls.setUpClass()
        return True
    except Timeout:
        raise
    except BaseException:
        return False


def class_teardown(cls):
    try:
        cls.tearDownClass()
    except BaseException:
        pass
    try:
        cls.doClassCleanups()
    except BaseException:
        pass


def run_case(cls, method, cases=None):
    """True when the test passes.  cases: only these fixture cases are walked."""
    CASE[0] = cases
    try:
        return _run_case(cls, method)
    finally:
        CASE[0] = None


def _run_case(cls, method):
    try:
        case = cls(method)
    except Timeout:
        raise
    except BaseException:
        return False
    res = unittest.TestResult()
    case.run(res)
    return not (res.errors or res.failures or res.unexpectedSuccesses)
