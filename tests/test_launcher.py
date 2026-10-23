"""Launcher: config, recipes, probes (pure) and the service with a fake reconstruction."""
import json
import time
from pathlib import Path

import numpy as np
import pytest
import yaml

from ptyguy.launcher import DONE, FAILED, LauncherService, load_launcher_config
from ptyguy.launcher.executors import render_command
from ptyguy.launcher.probes import ProbeLibrary
from ptyguy.launcher.recipes import select
from ptyguy.listener import ScanEvent, SpoolSink
from ptyguy.signals import ConfigError
from ptyguy.spool import Spool

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "examples" / "fake_recon.py"


def event(n=1, status="complete", probe=1.5, energy=700.0, **meta):
    md = {"probe_size": probe, "energy": energy, "scan_type": "FERMAT"}
    md.update(meta)
    return ScanEvent(uid="bl_%05d_20261023T120000" % n, beamline="bl", status=status,
                     scan_number=n, file_path="/d/scan_%05d.h5" % n, started=1.0,
                     finished=2.0, emitted=3.0, metadata=md, warnings=[])


@pytest.fixture
def env(tmp_path):
    """A probe library and a launcher config dict, all inside tmp_path."""
    probes = tmp_path / "probes"
    probes.mkdir()
    for name in ("p03.npy", "p15_700.npy", "p15_780.npy"):
        np.save(probes / name, np.ones((4, 4), dtype=np.complex64))
    (probes / "index.yaml").write_text(yaml.safe_dump({"probes": [
        {"file": "p03.npy", "probe_size": 0.3},                     # any energy
        {"file": "p15_700.npy", "probe_size": 1.5, "energy": 700},
        {"file": "p15_780.npy", "probe_size": 1.5, "energy": 780},
    ]}))
    run = '{python} "%s" {job}' % FAKE
    cfg = {
        "spool": str(tmp_path / "spool"), "analysis_root": str(tmp_path / "analysis"),
        "max_parallel": 1, "reconstruct": ["complete"],
        "snapshots": {"every": 5, "keep": 2},
        "probes": {"index": str(probes / "index.yaml")},
        "recipes": [
            {"name": "small", "match": {"probe_size": [0, 1.0]}, "run": run,
             "params": {"iterations": 10, "seconds": 0.3}},
            {"name": "big", "match": {"probe_size": [1.0, 5.0], "scan_type": ["FERMAT"]},
             "run": run, "params": {"iterations": 10, "seconds": 0.3}},
        ],
    }
    return tmp_path, cfg


def run_until(service, condition, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        service.step()
        if condition():
            return True
        time.sleep(0.05)
    return False


# -- config -------------------------------------------------------------------
def test_example_config_loads(tmp_path):
    raw = yaml.safe_load((ROOT / "examples" / "launcher.yaml").read_text())
    raw["probes"]["index"] = None                    # library may not be built yet
    for r in raw["recipes"]:
        r["probe"] = "none"
    cfg = load_launcher_config(raw)
    assert [r.name for r in cfg.recipes] == ["small-probe", "large-probe"]


def test_config_reports_all_problems(env):
    _, cfg = env
    cfg["max_parallel"] = 0
    cfg["reconstruct"] = ["complete", "maybe"]
    cfg["recipes"][0]["run"] = "python x.py"         # no {job}
    cfg["recipes"][1]["match"]["probe_size"] = [5, 1]
    cfg["colour"] = "blue"
    with pytest.raises(ConfigError) as e:
        load_launcher_config(cfg)
    text = str(e.value)
    for expected in ["max_parallel", "'maybe'", "must contain {job}", "lo > hi", "'colour'"]:
        assert expected in text, expected


def test_unquoted_braces_give_a_hint(tmp_path):
    p = tmp_path / "l.yaml"
    p.write_text("recipes:\n  - name: a\n    run: {python} x.py {job}\n")
    with pytest.raises(ConfigError, match="must be quoted"):
        load_launcher_config(p)


def test_relative_paths_are_relative_to_the_file(tmp_path):
    (tmp_path / "sub").mkdir()
    p = tmp_path / "sub" / "launcher.yaml"
    p.write_text('spool: ../spool\nrecipes:\n  - {name: a, run: "x {job}", probe: none}\n')
    cfg = load_launcher_config(p)
    assert cfg.spool == (tmp_path / "spool").resolve() and cfg.base_dir == p.parent


# -- recipes and probes (pure) ------------------------------------------------
def test_recipe_selection(env):
    _, raw = env
    cfg = load_launcher_config(raw)
    assert select(cfg.recipes, event(probe=0.3))[0].name == "small"
    assert select(cfg.recipes, event(probe=1.5))[0].name == "big"
    recipe, why = select(cfg.recipes, event(probe=9.0))
    assert recipe is None and "small: probe_size=9 not in [0, 1.0]" in why
    recipe, why = select(cfg.recipes, event(probe=1.5, scan_type="RASTER"))
    assert recipe is None and "scan_type=RASTER" in why
    recipe, why = select(cfg.recipes, event(probe=None))
    assert recipe is None and "probe_size unknown" in why


def test_probe_library(env):
    tmp, _ = env
    lib = ProbeLibrary(tmp / "probes" / "index.yaml", 0.2, 5.0)
    assert lib.choose(1.497, 700.0)[0].endswith("p15_700.npy")
    assert lib.choose(1.5, 781.0)[0].endswith("p15_780.npy")
    assert lib.choose(0.32, 1200.0)[0].endswith("p03.npy")      # no energy: fits any
    path, why = lib.choose(1.5, 900.0)
    assert path is None and "no probe within" not in why        # p03 fits energy, size off
    assert "off" in why
    assert lib.choose(None, 700.0) == (None, "no probe_size in event")


def test_render_command_quoting():
    assert render_command('x {job}', "/a b/j.json") == 'x "/a b/j.json"'
    assert render_command('m -batch "r(\'{job}\')"', "/a/j.json") == 'm -batch "r(\'/a/j.json\')"'


# -- service with the fake reconstruction --------------------------------------
def test_event_to_finished_job(env):
    tmp, raw = env
    service = LauncherService(load_launcher_config(raw))
    SpoolSink(Spool(raw["spool"])).emit(event(probe=1.497))
    done = tmp / "spool" / "done"
    assert run_until(service, lambda: not service.busy and list(done.glob("*.json")))

    job_dir = tmp / "analysis" / "bl_00001_20261023T120000"
    job = json.loads((job_dir / "job.json").read_text())
    status = json.loads((job_dir / "status.json").read_text())
    assert job["recipe"] == "big" and job["params"]["iterations"] == 10
    assert job["probe"]["path"].endswith("p15_700.npy") and "0.2 % off" in job["probe"]["reason"]
    assert job["event"]["scan_number"] == 1
    assert status["state"] == DONE and status["exit_code"] == 0
    assert json.loads((job_dir / "progress.json").read_text())["iteration"] == 10
    assert (job_dir / "result.h5").exists()
    assert not (job_dir / "snapshots").exists()       # cleanup: on_success
    assert "fake recon of /d/scan_00001.h5" in (job_dir / "job.log").read_text()


def test_skipped_and_unmatched_events(env):
    tmp, raw = env
    service = LauncherService(load_launcher_config(raw))
    sink = SpoolSink(Spool(raw["spool"]))
    sink.emit(event(1, status="aborted"))
    sink.emit(event(2, probe=9.0))
    service.step()
    assert "skipped: status aborted" in (tmp / "spool" / "done" / "bl_00001_20261023T120000.log").read_text()
    assert "no recipe matches" in (tmp / "spool" / "failed" / "bl_00002_20261023T120000.err").read_text()
    assert not (tmp / "analysis").exists() or not any((tmp / "analysis").iterdir())


def test_failing_reconstruction_keeps_snapshots(env):
    tmp, raw = env
    raw["recipes"][1]["params"]["fail"] = True
    service = LauncherService(load_launcher_config(raw))
    SpoolSink(Spool(raw["spool"])).emit(event(probe=1.5))
    assert run_until(service, lambda: list((tmp / "spool" / "failed").glob("*.json")))
    job_dir = tmp / "analysis" / "bl_00001_20261023T120000"
    status = json.loads((job_dir / "status.json").read_text())
    assert status["state"] == FAILED and status["exit_code"] == 3
    assert list((job_dir / "snapshots").glob("iter_*.json"))      # kept for debugging
    assert "exit code 3" in (tmp / "spool" / "failed" / "bl_00001_20261023T120000.err").read_text()


def test_max_parallel_and_rerun(env):
    tmp, raw = env
    service = LauncherService(load_launcher_config(raw))
    sink = SpoolSink(Spool(raw["spool"]))
    sink.emit(event(1, probe=0.3))
    sink.emit(event(2, probe=0.3))
    service.step()
    assert len(service.running) == 1 and len(service.queued) == 1      # max_parallel: 1
    assert run_until(service, lambda: not service.busy)

    # re-run scan 1: copy its event back into incoming/ -> new folder, old one untouched
    done = tmp / "spool" / "done" / "bl_00001_20261023T120000.json"
    (tmp / "spool" / "incoming" / done.name).write_text(done.read_text())
    assert run_until(service, lambda: (tmp / "analysis" / "bl_00001_20261023T120000_r2"
                                       / "status.json").exists() and not service.busy)
    names = sorted(p.name for p in (tmp / "analysis").iterdir())
    assert names == ["bl_00001_20261023T120000", "bl_00001_20261023T120000_r2",
                     "bl_00002_20261023T120000"]


def test_leftover_claimed_events_are_reported(env, caplog):
    tmp, raw = env
    spool = Spool(raw["spool"])
    (spool.root / "claimed" / "old_event.json").write_text("{}")
    with caplog.at_level("WARNING"):
        LauncherService(load_launcher_config(raw))
    assert "old_event.json" in caplog.text
