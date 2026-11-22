#!/bin/sh
# Rebuild maps/ from the pinned sqlglot checkout.
#   tools/build_maps.sh <sqlglot_root> <tests.json>
# Inside the image (make maps) the real duckdb is used for one of the two
# plain runs, which makes the duckdb-backed optimizer tests checkable too.
set -e
SG=$1
TESTS=$2
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=${BR_BUILD:-/tmp/br-build}
mkdir -p "$OUT"
PYTHONHASHSEED=1 python "$HERE/tools/build_map.py" plain "$SG" "$TESTS" "$OUT/plain1.pkl"
PYTHONHASHSEED=77 BR_SHIM=1 python "$HERE/tools/build_map.py" plain "$SG" "$TESTS" "$OUT/plain2.pkl"
BR_SHIM=1 python "$HERE/tools/build_map.py" trace "$SG" "$TESTS" "$OUT"
rm -f "$OUT/noisy.pkl"
for seed in 1 2 3; do PYTHONHASHSEED=$seed python "$HERE/tools/noise.py" "$SG" "$OUT/plain1.pkl" "$OUT/noisy.pkl"; done
rm -rf "$OUT/cov" "$OUT/case"
python "$HERE/tools/build_map.py" merge "$SG" "$OUT" "$OUT/plain1.pkl" "$OUT/plain2.pkl"
# comparison relations and table-key reads (maps/rel.bin)
for i in 0 1 2 3; do
  PYTHONHASHSEED=0 python "$HERE/tools/build_rel.py" run "$SG" "$TESTS" "$OUT/rel_$i.pkl" $i 4 &
done
wait
for t in test_execute_tpch test_optimized_tpch test_execute_tpcds; do
  PYTHONHASHSEED=0 BR_REL_SKIP=none BR_REL_ONLY=$t python "$HERE/tools/build_rel.py" run "$SG" "$TESTS" "$OUT/rel_$t.pkl"
done
python "$HERE/tools/build_rel.py" merge "$TESTS" "$OUT/rel.bin" "$OUT"/rel_*.pkl
rm -rf "$HERE/maps"
mkdir -p "$HERE/maps"
cp -r "$OUT/cov" "$OUT/case" "$OUT"/*.bin "$HERE/maps/"
echo "maps rebuilt in $HERE/maps"
