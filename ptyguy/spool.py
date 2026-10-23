"""Spool folder: the queue between listener and launcher.

    spool/
      incoming/   new events (listener writes here)
      claimed/    taken by a launcher
      done/       handled
      failed/     handling failed; an .err file next to it says why

Two rules make it safe:
  - write atomically: write a hidden temp file, then rename it. Readers never
    see half a file.
  - claim by rename: moving incoming/ -> claimed/ succeeds for exactly one
    launcher, so the move is the lock.
"""
import os
from pathlib import Path

STATES = ("incoming", "claimed", "done", "failed")


class Spool:
    def __init__(self, root):
        self.root = Path(root).resolve()
        for state in STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)

    def path(self, state, name):
        return self.root / state / name

    def put(self, text, name):
        """Atomically create incoming/<name>. Returns its path."""
        final = self.path("incoming", name)
        tmp = self.path("incoming", "." + name + ".tmp")
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())          # on disk before anyone can see the name
        os.replace(tmp, final)            # atomic, also overwrites on Windows
        return final

    def pending(self):
        """Names in incoming/, oldest first. Temp files are ignored."""
        d = self.root / "incoming"
        names = [p for p in d.iterdir() if p.suffix == ".json" and not p.name.startswith(".")]
        return [p.name for p in sorted(names, key=lambda p: p.stat().st_mtime)]

    def claim(self, name):
        """incoming/ -> claimed/. Returns the claimed path, or None if someone was faster."""
        try:
            os.rename(self.path("incoming", name), self.path("claimed", name))
        except OSError:
            return None
        return self.path("claimed", name)

    def finish(self, name, ok=True, message=""):
        """claimed/ -> done/ or failed/ (+ <name>.err with the message)."""
        state = "done" if ok else "failed"
        os.replace(self.path("claimed", name), self.path(state, name))
        if message:
            suffix = ".log" if ok else ".err"
            self.path(state, name).with_suffix(suffix).write_text(message)
        return self.path(state, name)
