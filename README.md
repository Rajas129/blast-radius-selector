# Blast Radius selector

For each patch against sqlglot `eb11aec`, print which tests to run and how to
split them over workers.

## How it decides

1. **Maps of the pinned commit** (`maps/`, built by `tools/build_map.py`).
   Every test is run once under `sys.monitoring` and we record, per test:
   function lines executed, files opened, folders listed, classes
   instantiated, modules imported, and for fixture-driven tests the lines each
   fixture case reaches. Nothing in the maps comes from a patch.
2. **Static analysis** (`br/analyze.py`). The patch is applied in memory and
   old and new source are compared statement by statement. Comment, layout and
   docstring edits select nothing. An edit inside a function selects the tests
   that ran that statement. An edit that runs at import time (class body,
   module level, signature) is passed to the state check. A data file selects
   the tests that open it. pytest configuration selects everything.
3. **State check** (`br/state.py`). For import-time edits the patched package
   is re-imported in a forked child and every module global and class
   attribute is fingerprinted against the pinned state. Only tests that use a
   class whose attribute changed, and run a line that names that attribute,
   stay candidates.
4. **Check by running** (`br/child.py`). The child is forked from a parent
   that already imported sqlglot. Patched function bodies are swapped into the
   live functions (or everything is re-imported when that is not safe),
   patched files are served from memory, and cheap candidates are run with
   `unittest`. Passing tests are dropped. Running stops when the CPU still
   needed costs more than the schedule seconds it can still save.
5. **Heavy tests are never run** (`br/heavy.py`). `test_max_nodes` and
   `test_generator_memory` are run with smaller loop constants. The three TPC
   tests are dropped only when the DuckDB SQL and the optimized tree of every
   query the change can reach are identical to the pinned commit, and the
   change reaches nothing that runs after optimization.
6. **Packing** (`br/pack.py`): longest first over 1 to 8 workers with move/swap
   improvement; dropped candidates are slipped into idle worker time.

Anything that cannot be settled stays selected. Any error selects every test
for that commit; the program never exits without an answer for every commit.

## Use

    make sample   # build, run under the scorer's limits, score, per-commit waste
    make maps     # rebuild maps/ inside the image (about ten minutes), then commit maps/

`run.log` holds the selector's reasoning for the last `make sample`.
