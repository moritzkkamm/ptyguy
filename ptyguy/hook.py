"""The reconstruction side of PtyGuy: one object to call from inside your engine.

    from ptyguy.hook import Hook

    hook = Hook()                                  # job.json from argv[1] or $PTYGUY_JOB
    for it in range(1, n + 1):
        ...                                        # your engine's iteration
        hook.update(it, error, obj=obj, probe=probe)
    hook.finish(obj, probe)

update() is meant to be called EVERY iteration and is cheap:
  - progress.json   (iteration, error, heartbeat) at most every `progress_s` seconds
  - snapshots/iter_NNNNNN.h5 every `snapshots.every` iterations (from job.json),
    only the newest `snapshots.keep` are kept, object decimated by `downsample`
finish() writes result.h5 with the full-resolution object, probe and error history.

All files are written atomically (temp file + rename), so the plotter never
reads half a file. Arrays may be numpy or cupy (anything with .get()).
"""
import json
import os
import sys
import time
from pathlib import Path

import h5py
import numpy as np


def _to_numpy(a):
    if a is None:
        return None
    if hasattr(a, "get"):                      # cupy -> numpy
        a = a.get()
    return np.asarray(a)


def _replace_atomic(write, final):
    """write(tmp_path) then rename tmp -> final."""
    final = Path(final)
    tmp = final.with_name("." + final.name + ".tmp")
    write(tmp)
    os.replace(tmp, final)


def load_job(job_path=None):
    """job.json as a dict: from the argument, argv[1], or $PTYGUY_JOB."""
    job_path = job_path or (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PTYGUY_JOB"))
    if not job_path:
        raise RuntimeError("no job.json given (argument or PTYGUY_JOB)")
    return json.loads(Path(job_path).read_text())


class Hook:
    def __init__(self, job=None, progress_s=0.5, total=None):
        self.job = job if isinstance(job, dict) else load_job(job)
        snaps = self.job.get("snapshots", {})
        self.every = max(1, int(snaps.get("every", 20)))
        self.keep = max(1, int(snaps.get("keep", 3)))
        self.downsample = max(1, int(snaps.get("downsample", 1)))
        paths = self.job["paths"]
        self.snapshot_dir = Path(paths["snapshots"])
        self.progress_path = Path(paths["progress"])
        self.result_path = Path(paths["result"])
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.progress_s = progress_s
        self.total = total or self.job.get("params", {}).get("iterations")
        self.errors = []
        self.t0 = time.time()
        self._last_progress = 0.0

    # ------------------------------------------------------------------
    def update(self, iteration, error, obj=None, probe=None):
        self.errors.append(float(error))
        now = time.time()
        if now - self._last_progress >= self.progress_s:
            self._write_progress(iteration, error, state="running")
        if iteration % self.every == 0 and obj is not None:
            self.snapshot(iteration, obj, probe)

    def snapshot(self, iteration, obj, probe=None):
        obj = _to_numpy(obj)[..., ::self.downsample, ::self.downsample]
        probe = _to_numpy(probe)

        def write(path):
            with h5py.File(path, "w") as f:
                f["object"] = obj.astype(np.complex64)
                if probe is not None:
                    f["probe"] = probe.astype(np.complex64)
                f["error"] = np.asarray(self.errors, dtype=np.float64)
                f.attrs.update(iteration=iteration, downsample=self.downsample,
                               time=time.time(), uid=self.job.get("uid", ""))
                if self.total:
                    f.attrs["iterations"] = int(self.total)

        _replace_atomic(write, self.snapshot_dir / ("iter_%06d.h5" % iteration))
        for old in sorted(self.snapshot_dir.glob("iter_*.h5"))[:-self.keep]:
            old.unlink()

    def finish(self, obj, probe=None, **attrs):
        """Full-resolution result + final progress. Extra attrs go into result.h5."""
        obj, probe = _to_numpy(obj), _to_numpy(probe)

        def write(path):
            with h5py.File(path, "w") as f:
                f["object"] = obj.astype(np.complex64)
                if probe is not None:
                    f["probe"] = probe.astype(np.complex64)
                f["error"] = np.asarray(self.errors, dtype=np.float64)
                f.attrs.update(iterations=len(self.errors), seconds=time.time() - self.t0,
                               uid=self.job.get("uid", ""), **attrs)

        _replace_atomic(write, self.result_path)
        self._write_progress(len(self.errors), self.errors[-1] if self.errors else None,
                             state="finished")

    # ------------------------------------------------------------------
    def _write_progress(self, iteration, error, state):
        self._last_progress = time.time()
        data = {"iteration": iteration, "iterations": self.total, "error": error,
                "state": state, "heartbeat": self._last_progress,
                "elapsed": self._last_progress - self.t0}
        _replace_atomic(lambda p: p.write_text(json.dumps(data)), self.progress_path)
