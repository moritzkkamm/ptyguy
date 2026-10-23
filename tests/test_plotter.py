"""Plotter: reading job folders robustly, phase display, and the matplotlib view (headless)."""
import json
import time
from types import SimpleNamespace

import matplotlib
import numpy as np
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ptyguy.hook import Hook  # noqa: E402
from ptyguy.plotter import JobReader, display_phase, list_jobs, pick_follow  # noqa: E402
from ptyguy.plotter.mpl import Watcher  # noqa: E402


def make_job(root, name, state, queued, scan=1, iterations=10, finish=False, warnings=()):
    d = root / name
    (d / "snapshots").mkdir(parents=True)
    job = {"uid": name, "event": {"scan_number": scan, "warnings": list(warnings)},
           "params": {"iterations": iterations},
           "snapshots": {"every": 2, "keep": 2, "downsample": 1},
           "paths": {"snapshots": str(d / "snapshots"), "progress": str(d / "progress.json"),
                     "result": str(d / "result.h5")}}
    (d / "job.json").write_text(json.dumps(job))
    (d / "status.json").write_text(json.dumps(
        {"state": state, "recipe": "r", "queued": queued, "started": queued}))
    hook = Hook(job, progress_s=0)
    obj = np.exp(1j * np.where(np.arange(64 * 64).reshape(64, 64) % 7 < 3, 0.5, -0.5))
    for it in range(1, iterations + 1):
        hook.update(it, 1.0 / it, obj=obj, probe=np.ones((16, 16)))
    if finish:
        hook.finish(obj, np.ones((16, 16)))
    return d


def test_list_and_follow(tmp_path):
    make_job(tmp_path, "bl_00001_a", "done", 1.0, finish=True)
    make_job(tmp_path, "bl_00002_b", "running", 2.0)
    make_job(tmp_path, "bl_00003_c", "queued", 3.0)
    (tmp_path / "not_a_job").mkdir()
    jobs = list_jobs(tmp_path)
    assert [j.name for j in jobs] == ["bl_00003_c", "bl_00002_b", "bl_00001_a"]
    assert pick_follow(jobs).name == "bl_00002_b"              # newest RUNNING
    assert pick_follow([j for j in jobs if j.state != "running"]).name == "bl_00003_c"
    assert pick_follow([]) is None


def test_reader_prefers_result_and_survives_deleted_snapshots(tmp_path):
    d = make_job(tmp_path, "bl_00002_b", "running", 2.0)
    reader = JobReader(list_jobs(tmp_path)[0])
    view = reader.read()
    assert view.arrays.source == "snapshot iter_000010.h5" and view.arrays.iteration == 10
    assert view.scan_number == 1 and view.progress["iteration"] == 10

    for p in (d / "snapshots").iterdir():                      # rolling deletion
        p.unlink()
    assert reader.read().arrays.iteration == 10                # last good data kept

    Hook(json.loads((d / "job.json").read_text())).finish(np.ones((8, 8)), None)
    view = reader.read()
    assert view.arrays.source == "result.h5" and view.arrays.obj.shape == (8, 8)


def test_notes_stale_and_failed(tmp_path):
    d = make_job(tmp_path, "bl_00001_a", "running", 1.0, warnings=["energy: PV 703"])
    reader = JobReader(list_jobs(tmp_path)[0])
    notes = reader.read(now=time.time() + 120).notes
    assert "energy: PV 703" in notes and any("no progress for" in n for n in notes)
    (d / "status.json").write_text(json.dumps({"state": "failed", "message": "exit code 3",
                                               "queued": 1.0}))
    assert "exit code 3" in reader.read().notes


def test_display_phase():
    levels = np.where(np.arange(100) % 2, 0.6, -0.6).reshape(10, 10)
    obj = np.exp(1j * (levels + 1.0))                           # global offset of 1 rad
    phase, lim = display_phase(obj)
    assert np.allclose(np.abs(phase), 0.6, atol=1e-6) and lim == pytest.approx(0.6)
    phase, lim = display_phase(obj, wrapped=True)
    assert lim == np.pi and phase.max() == pytest.approx(1.6)


def test_watcher_draws_and_switches(tmp_path):
    make_job(tmp_path, "bl_00001_a", "done", 1.0, scan=1, finish=True)
    make_job(tmp_path, "bl_00002_b", "running", 2.0, scan=2)
    w = Watcher(tmp_path, plt=plt)
    view = w.refresh()
    assert view.scan_number == 2                                # following the running one
    assert "scan 2" in w.header.get_text() and "RUNNING" in w.header.get_text()
    assert w.im_phase.get_array().shape == (64, 64)
    assert "[00002 running]" in w.footer.get_text()

    w._on_key(SimpleNamespace(key="left"))                      # older job, stop following
    assert not w.follow and "scan 1" in w.header.get_text()
    assert "result.h5" in w.header.get_text()
    w._on_key(SimpleNamespace(key="w"))
    assert w.im_phase.get_cmap().name == "twilight"
    w._on_key(SimpleNamespace(key="f"))
    assert w.follow and "scan 2" in w.header.get_text()
    w.fig.savefig(tmp_path / "watch.png")                        # renders without errors
    plt.close(w.fig)


def test_watcher_without_jobs(tmp_path):
    w = Watcher(tmp_path / "nothing_yet", plt=plt)
    assert w.refresh() is None and "waiting for jobs" in w.header.get_text()
    plt.close(w.fig)
