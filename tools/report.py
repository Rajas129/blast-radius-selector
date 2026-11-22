"""Per-commit waste against the best possible selection (sample set only).

    python3 tools/report.py sample output.jsonl
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import pack  # noqa: E402

sample, out = sys.argv[1], sys.argv[2]
tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"].split("::")[-1] for t in tests}
answers = json.load(open(os.path.join(sample, "answers.json")))["commits"]


def cost(workers):
    return (max(sum(ms[i] for i in w) for w in workers) + 2000 * len(workers)) / 1000 if workers else 0.0


rows = []
total = floor = 0.0
for line in open(out):
    d = json.loads(line)
    w = [x for x in d["workers"] if x]
    fail = answers[d["commit"]]["failing"]
    c, b = cost(w), cost(pack.pack(fail, ms))
    sel = {i for x in w for i in x}
    total += c
    floor += b
    heavy = [name[i] for i in sel if ms[i] > 2500 and i not in fail]
    rows.append((c - b, d["commit"], c, b, len(sel), len(fail), "MISS" if set(fail) - sel else "", heavy))
print("selection %.2f s, floor %.2f s" % (total, floor))
for r in sorted(rows, reverse=True):
    if r[0] > 0.005 or r[6]:
        print("  waste %6.2f  %s  cost %.2f  best %.2f  selected %d  failing %d %s %s" % r)
