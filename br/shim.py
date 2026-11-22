"""Stand-in modules for duckdb / pandas / numpy.

Most sqlglot tests import these at module level but never call them.  The
stand-ins make those imports free.  Any real use flips ``USED`` so the runner
knows the verdict of that test cannot be trusted.
"""
import sys
import types

USED = [0]


class Magic:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __hash__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return True

    def __call__(self, *a, **k):
        return self

    def __getitem__(self, k):
        return self

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn(Magic):
    def execute(self, *a, **k):
        USED[0] += 1
        return Magic()

    sql = query = executemany = execute


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    m.__br_shim__ = True
    return m


def _used(*a, **k):
    USED[0] += 1
    return Magic()


def make():
    duck = _mod("duckdb", connect=lambda *a, **k: Conn(), sql=_used, query=_used)
    np = _mod("numpy", nan=float("nan"))
    np.__getattr__ = lambda name: _used
    pdt = _mod("pandas.testing", assert_frame_equal=_used)
    pd = _mod("pandas", DataFrame=_used, testing=pdt)
    pd.__getattr__ = lambda name: _used
    return {"duckdb": duck, "numpy": np, "pandas": pd, "pandas.testing": pdt}


def _pytz():
    import datetime
    import zoneinfo

    class _TZ:
        def __init__(self, name):
            self.zone = zoneinfo.ZoneInfo(name)

        def localize(self, dt):
            return dt.replace(tzinfo=self.zone)

    return _mod("pytz", timezone=_TZ, utc=datetime.timezone.utc, UTC=datetime.timezone.utc)


def install(numpy=False):
    """Put the stand-ins in sys.modules.  numpy=True keeps the real numpy."""
    mods = make()
    if numpy:
        del mods["numpy"]
    for k in [k for k in sys.modules if k.split(".")[0] in mods]:
        del sys.modules[k]
    sys.modules.update(mods)
    try:
        import pytz  # noqa: F401
    except Exception:
        sys.modules["pytz"] = _pytz()
