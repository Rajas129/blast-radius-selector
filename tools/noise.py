"""Find state keys that differ between two clean imports of the pinned commit."""
import importlib
import os
import pickle
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.realpath(sys.argv[1]))
from br import state  # noqa: E402

mods = pickle.load(open(sys.argv[2], "rb"))["mods"]
noisy = set()
loaded = []
for n in mods:
    try:
        importlib.import_module(n)
        loaded.append(n)
    except Exception:
        pass
a = state.snapshot(loaded)
for _ in range(3):
    for k in list(sys.modules):
        if k == "sqlglot" or k.startswith("sqlglot."):
            del sys.modules[k]
    for n in loaded:
        importlib.import_module(n)
    b = state.snapshot(loaded)
    noisy |= {c[:3] for c in state.diff(a, b)}
old = set()
if os.path.exists(sys.argv[3]):
    old = pickle.load(open(sys.argv[3], "rb"))
pickle.dump(noisy | old, open(sys.argv[3], "wb"))
print("noisy keys:", sorted(noisy | old), file=sys.stderr)
