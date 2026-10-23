"""Job folders: one per reconstruction.

    analysis/<uid>/          (<uid>_r2, _r3, ... for re-runs: nothing is overwritten)
      job.json       launcher writes once: everything the reconstruction needs
      status.json    launcher writes: queued / running / done / failed
      progress.json  the reconstruction (hook) writes: iteration, error, heartbeat
      job.log        stdout + stderr of the reconstruction
      snapshots/     the reconstruction (hook) writes, every N iterations
      result.h5      the reconstruction writes at the end
"""
import json
import os
import shutil
import time
from pathlib import Path

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


def write_json_atomic(path, obj):
    """Readers (plotter, GUI, you) never see half a file."""
    path = Path(path)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def new_job_dir(analysis_root, uid):
    """analysis/<uid>, or <uid>_r2, _r3 ... if it exists. Created atomically (mkdir)."""
    root = Path(analysis_root)
    root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = uid if n == 1 else "%s_r%d" % (uid, n)
        try:
            (root / name).mkdir()
            return root / name
        except FileExistsError:
            n += 1


class Job:
    def __init__(self, job_dir, spool_name, recipe, command):
        self.dir = Path(job_dir)
        self.spool_name = spool_name       # spool item stays in claimed/ until we finish
        self.recipe = recipe
        self.command = command
        self.handle = None                 # executor's process handle
        self.status = {"state": QUEUED, "recipe": recipe.name, "command": command,
                       "queued": time.time(), "started": None, "ended": None,
                       "pid": None, "exit_code": None, "message": ""}

    @property
    def job_json(self):
        return self.dir / "job.json"

    @property
    def snapshot_dir(self):
        return self.dir / "snapshots"

    def write_status(self, **changes):
        self.status.update(changes)
        write_json_atomic(self.dir / "status.json", self.status)

    def cleanup_snapshots(self):
        shutil.rmtree(self.snapshot_dir, ignore_errors=True)
