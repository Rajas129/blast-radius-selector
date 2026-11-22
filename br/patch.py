"""Parse a unified diff and apply it in memory."""
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary", "added", "deleted", "renamed")

    def __init__(self):
        self.old = self.new = None
        self.hunks = []
        self.binary = self.added = self.deleted = self.renamed = False


def _strip(p):
    p = p.strip()
    if "\t" in p:
        p = p.split("\t")[0]
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            rest = ln[11:]
            m = re.match(r'^"?a/(.*?)"? "?b/(.*?)"?$', rest)
            if m:
                half = (len(rest) - 1) // 2
                if rest[:half] == "a/" + rest[half + 3:]:
                    cur.old = cur.new = rest[2:half]
                else:
                    cur.old, cur.new = m.group(1), m.group(2)
        elif ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            o, w = _strip(ln[4:]), _strip(lines[i + 1][4:])
            if o is None:
                cur.added = True
            else:
                cur.old = o
            if w is None:
                cur.deleted = True
            else:
                cur.new = w
            i += 1
        elif cur is not None and ln.startswith("@@ "):
            m = _HUNK.match(ln)
            if not m:
                raise ValueError("bad hunk header")
            body = []
            o = int(m.group(2)) if m.group(2) is not None else 1
            w = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            while i < n and (o > 0 or w > 0):
                h = lines[i]
                c = h[:1]
                if c == "\\":
                    body.append(h)
                elif c == "-":
                    o -= 1
                    body.append(h)
                elif c == "+":
                    w -= 1
                    body.append(h)
                else:  # context; some tools strip the single space of blank lines
                    o -= 1
                    w -= 1
                    body.append(h if c == " " else " " + h)
                i += 1
            if i < n and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur.hunks.append((int(m.group(1)), body))
            continue
        elif cur is not None:
            if ln.startswith("new file mode"):
                cur.added = True
            elif ln.startswith("deleted file mode"):
                cur.deleted = True
            elif ln.startswith("rename from "):
                cur.renamed = True
                cur.old = ln[12:].strip()
            elif ln.startswith("rename to "):
                cur.renamed = True
                cur.new = ln[10:].strip()
            elif ln.startswith(("copy from ", "copy to ")):
                cur.added = True
                if ln.startswith("copy to "):
                    cur.new = ln[8:].strip()
            elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
                cur.binary = True
        i += 1
    return files


def apply(old_text, hunks):
    """Returns (new_text, edits); edits are (old_start, old_end, new_start, new_end), 1-based, end exclusive."""
    old = old_text.split("\n")
    out = []
    edits = []
    pos = 0  # index into old
    for start, body in hunks:
        want = [h[1:] for h in body if h[:1] in (" ", "-")]
        at = max(start - 1, 0) if want else start
        if old[at:at + len(want)] != want:
            found = None
            for d in range(1, 200):
                for cand in (at - d, at + d):
                    if cand >= pos and old[cand:cand + len(want)] == want:
                        found = cand
                        break
                if found is not None:
                    break
            if found is None:
                raise ValueError("hunk does not apply")
            at = found
        if at < pos:
            raise ValueError("overlapping hunks")
        out.extend(old[pos:at])
        pos = at
        blk = None
        for h in body:
            c = h[:1]
            if c == " ":
                out.append(old[pos])
                pos += 1
                blk = None
            elif c == "-":
                if blk is None:
                    blk = [pos + 1, pos + 1, len(out) + 1, len(out) + 1]
                    edits.append(blk)
                pos += 1
                blk[1] = pos + 1
            elif c == "+":
                if blk is None:
                    blk = [pos + 1, pos + 1, len(out) + 1, len(out) + 1]
                    edits.append(blk)
                out.append(h[1:])
                blk[3] = len(out) + 1
            elif c == "\\":
                # "\ No newline at end of file" applies to the line just before it
                pass
    out.extend(old[pos:])
    new_text = "\n".join(out)
    # end-of-file newline handling
    flat = [h for _, body in hunks for h in body]
    for j, h in enumerate(flat):
        if h.startswith("\\") and j > 0:
            prev = flat[j - 1][:1]
            if prev in ("+", " ") and new_text.endswith("\n"):
                last_new = [k for k, x in enumerate(flat) if x[:1] in ("+", " ")][-1]
                if last_new == j - 1:
                    new_text = new_text[:-1]
            elif prev == "-" and not new_text.endswith("\n") and not any(
                    x.startswith("\\") for x in flat[j + 1:]):
                new_text += "\n"
    return new_text, [tuple(e) for e in edits]
