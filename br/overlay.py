"""Serve patched files from memory: the disk is read-only."""
import builtins
import importlib.abc
import importlib.util
import io
import os
import sys


class Loader(importlib.abc.SourceLoader):
    def __init__(self, name, path, text):
        self.name, self.path, self.text = name, path, text

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if path == self.path:
            return self.text.encode("utf-8", "surrogateescape")
        with open(path, "rb") as fh:
            return fh.read()

    def path_stats(self, path):
        raise OSError("no cache for patched source")


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root, files):
        self.mods = {}
        for r, text in files.items():
            if not r.endswith(".py"):
                continue
            name = r[:-3].replace("/", ".")
            pkg = name.endswith(".__init__")
            if pkg:
                name = name[:-9]
            self.mods[name] = (os.path.join(root, r), text, pkg)

    def find_spec(self, fullname, path=None, target=None):
        ent = self.mods.get(fullname)
        if ent is None:
            return None
        p, text, pkg = ent
        if text is None:
            raise ModuleNotFoundError("No module named %r" % fullname, name=fullname)
        return importlib.util.spec_from_file_location(
            fullname, p, loader=Loader(fullname, p, text),
            submodule_search_locations=[os.path.dirname(p)] if pkg else None,
        )


def install(root, files):
    """files: {relative path: new text, or None when the patch deletes it}."""
    sys.meta_path.insert(0, Finder(root, files))
    table = {os.path.join(root, r): t for r, t in files.items()}
    real = builtins.open

    def opener(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)):
            p = os.path.abspath(os.fspath(file))
            if p in table and not any(c in mode for c in "wax+"):
                t = table[p]
                if t is None:
                    raise FileNotFoundError(2, "No such file or directory", p)
                if "b" in mode:
                    return io.BytesIO(t.encode("utf-8", "surrogateescape"))
                if k.get("newline") is None and (len(a) < 4 or a[3] is None):
                    t = t.replace("\r\n", "\n").replace("\r", "\n")
                return io.StringIO(t)
        return real(file, mode, *a, **k)

    builtins.open = opener
    io.open = opener
