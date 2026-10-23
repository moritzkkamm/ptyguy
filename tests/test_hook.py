"""ptyguy.hook: cadence, rolling snapshots, downsampling, atomic files, result."""
import json
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

from ptyguy.hook import Hook
from ptyguy.signals import load_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def job(tmp_path):
    d = tmp_path / "job"
    j = {"uid": "bl_00001_x", "params": {"iterations": 12},
         "snapshots": {"every": 3, "keep": 2, "downsample": 2},
         "paths": {"snapshots": str(d / "snapshots"), "progress": str(d / "progress.json"),
                   "result": str(d / "result.h5")}}
    d.mkdir()
    (d / "job.json").write_text(json.dumps(j))
    return d


def test_snapshots_progress_and_result(job):
    hook = Hook(str(job / "job.json"), progress_s=0)
    obj = np.ones((40, 50), np.complex64)
    probe = np.ones((8, 8), np.complex64)
    for it in range(1, 13):
        hook.update(it, 1.0 / it, obj=obj * it, probe=probe)

    snaps = sorted(p.name for p in (job / "snapshots").iterdir())
    assert snaps == ["iter_000009.h5", "iter_000012.h5"]           # every 3, keep 2
    with h5py.File(job / "snapshots" / "iter_000012.h5", "r") as f:
        assert f["object"].shape == (20, 25)                       # downsample 2
        assert f["probe"].shape == (8, 8)
        assert len(f["error"]) == 12 and f.attrs["iteration"] == 12
        assert f["object"][0, 0] == 12

    progress = json.loads((job / "progress.json").read_text())
    assert progress["iteration"] == 12 and progress["iterations"] == 12
    assert progress["state"] == "running"

    hook.finish(obj, probe, truth_error=0.1)
    with h5py.File(job / "result.h5", "r") as f:
        assert f["object"].shape == (40, 50)                       # full resolution
        assert f.attrs["truth_error"] == 0.1
    assert json.loads((job / "progress.json").read_text())["state"] == "finished"
    assert not list(job.rglob(".*.tmp"))                           # nothing half-written


def test_progress_is_throttled(job):
    hook = Hook(str(job / "job.json"), progress_s=60)
    hook.update(1, 0.5)
    hook.update(2, 0.4)                                            # within 60 s: skipped
    assert json.loads((job / "progress.json").read_text())["iteration"] == 1
    assert len(hook.errors) == 2                                   # history still complete


def test_epie_reconstructs_dummy_data(tmp_path):
    """The toy ePIE on simulated data: error must drop, result must resemble the truth."""
    subprocess.run([sys.executable, str(ROOT / "sim" / "simulate_data.py"), "--out",
                    str(tmp_path / "data"), "--probe-sizes", "1.0"], check=True,
                   capture_output=True)
    d = tmp_path / "job"
    layout = load_config(ROOT / "configs" / "dummy.yaml").data
    job = {"uid": "t", "event": {"file_path": str(tmp_path / "data" / "sim_probe_1.00um.h5"),
                                 "metadata": {"probe_size": 1.0, "energy": 700.0,
                                              "detector_distance": 0.25, "pixel_size": 75.0},
                                 "scan_number": 1, "data_layout": layout},
           "params": {"iterations": 15}, "probe": {"path": None, "reason": "test"},
           "snapshots": {"every": 5, "keep": 3, "downsample": 1},
           "paths": {"snapshots": str(d / "snapshots"), "progress": str(d / "progress.json"),
                     "result": str(d / "result.h5")}}
    d.mkdir()
    (d / "job.json").write_text(json.dumps(job))
    proc = subprocess.run([sys.executable, str(ROOT / "examples" / "epie.py"),
                           str(d / "job.json")], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    with h5py.File(d / "result.h5", "r") as f:
        err = f["error"][()]
        truth_error = f.attrs["truth_error"]
    assert err[-1] < err[0] / 5                       # converging
    assert truth_error < 0.3                          # rough sanity, not a benchmark
    assert sorted(p.name for p in (d / "snapshots").iterdir()) == [
        "iter_000005.h5", "iter_000010.h5", "iter_000015.h5"]
