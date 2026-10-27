"""Windows file locking, simulated: a reader holding a file makes replace/unlink fail briefly.

Regression: on Windows the plotter reading progress.json crashed the reconstruction
with PermissionError [WinError 5] in os.replace. Linux never shows this, so we fake it.
"""
import json
import os

import numpy as np
import pytest

from ptyguy import fsutil
from ptyguy.hook import Hook
from ptyguy.launcher.jobs import write_json_atomic


class Locked:
    """Make os.<name> raise PermissionError for the first `times` calls."""

    def __init__(self, monkeypatch, name, times):
        self.real, self.left, self.calls = getattr(os, name), times, 0
        monkeypatch.setattr(os, name, self)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.left != 0:
            self.left -= 1
            raise PermissionError(5, "Zugriff verweigert")
        return self.real(*args, **kwargs)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(fsutil, "DELAY", 0.001)


@pytest.fixture
def job(tmp_path):
    d = tmp_path / "job"
    (d / "snapshots").mkdir(parents=True)
    return {"uid": "x", "params": {"iterations": 10},
            "snapshots": {"every": 1, "keep": 1, "downsample": 1},
            "paths": {"snapshots": str(d / "snapshots"), "progress": str(d / "progress.json"),
                      "result": str(d / "result.h5")}}


def test_replace_retries_until_reader_lets_go(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("new")
    dst.write_text("old")
    locked = Locked(monkeypatch, "replace", times=3)
    fsutil.replace(src, dst, delay=0.001)
    assert dst.read_text() == "new" and locked.calls == 4


def test_replace_gives_up_eventually(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("x")
    Locked(monkeypatch, "replace", times=-1)                  # locked forever
    with pytest.raises(PermissionError):
        fsutil.replace(tmp_path / "a", tmp_path / "b", attempts=3, delay=0.001)


def test_progress_never_kills_the_reconstruction(job, monkeypatch, fast):
    hook = Hook(job, progress_s=0)
    Locked(monkeypatch, "replace", times=-1)                  # progress.json never writable
    hook.update(1, 0.5)                                       # must not raise
    assert hook.errors == [0.5]


def test_locked_old_snapshot_is_kept_and_deleted_later(job, monkeypatch, fast):
    hook = Hook(job, progress_s=0)
    obj = np.ones((4, 4), np.complex64)
    hook.update(1, 1.0, obj=obj)
    locked = Locked(monkeypatch, "unlink", times=-1)          # plotter has iter_1 open
    hook.update(2, 0.5, obj=obj)
    snaps = lambda: sorted(p.name for p in (job_dir(job) / "snapshots").iterdir())
    assert snaps() == ["iter_000001.h5", "iter_000002.h5"]    # survived, one too many
    monkeypatch.setattr(os, "unlink", locked.real)            # plotter let go
    hook.update(3, 0.3, obj=obj)
    assert snaps() == ["iter_000003.h5"]                      # cleaned up on the next round


def test_status_json_retries(tmp_path, monkeypatch, fast):
    Locked(monkeypatch, "replace", times=5)
    write_json_atomic(tmp_path / "status.json", {"state": "done"})
    assert json.loads((tmp_path / "status.json").read_text()) == {"state": "done"}


def job_dir(job):
    from pathlib import Path
    return Path(job["paths"]["progress"]).parent
