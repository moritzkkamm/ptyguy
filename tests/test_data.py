"""Data layout: validation, reading through the mapping, consistency checks, check-data."""
from pathlib import Path

import h5py
import numpy as np
import pytest

from ptyguy.cli import main as cli_main
from ptyguy.data import load_scan, parse_data_layout, read_scan
from ptyguy.listener import Obs, ScanTracker
from ptyguy.signals import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent
POS_UM = np.array([[0.0, 0.0], [0.5, 1.0], [1.0, -2.0]])          # (y, x) in um


def layout(**over):
    raw = {"frames": {"path": "d/frames"},
           "positions": {"path": "d/pos", "unit": "um", "order": "yx"},
           "geometry": {"energy": {"from": "event"}, "detector_distance": {"from": "event"},
                        "pixel_size": {"from": "event"}}}
    raw.update(over)
    problems = []
    out = parse_data_layout(raw, problems)
    assert not problems, problems
    return out


@pytest.fixture
def h5(tmp_path):
    """One file holding the same positions in several storage conventions."""
    p = tmp_path / "scan.h5"
    with h5py.File(p, "w") as f:
        f["d/frames"] = np.ones((3, 8, 8), np.uint32)
        f["d/pos"] = POS_UM                                         # (N,2) yx um
        f["d/pos_xy_nm"] = POS_UM[:, ::-1] * 1000                   # (N,2) xy nm
        f["d/pos_T"] = POS_UM.T                                     # (2,N)
        f["s/y_mm"] = POS_UM[:, 0] / 1000
        f["s/x_mm"] = POS_UM[:, 1] / 1000
        f["inst/energy_keV"] = 0.705                                # scalar dataset
        f["inst"].attrs.update(distance_mm=250.0, pixel_um=75.0)
    return p


META = {"energy": 700.0, "detector_distance": 0.25, "pixel_size": 75.0}


# -- validation ---------------------------------------------------------------
def test_layout_problems_reported_together():
    problems = []
    parse_data_layout({"format": "tiff", "positions": {"path": "p", "y": "y", "unit": "eV",
                                                       "order": "zx", "flip": ["z"]},
                       "geometry": {"energy": {"from": "file"}, "temperature": {}}},
                      problems, signals={"energy"})
    text = "\n".join(problems)
    for expected in ["'tiff'", "frames: 'path' is missing", "either 'path'", "unit 'eV' is a energy",
                     "order must be yx or xy", "only x and y", "from: file needs a 'path'",
                     "unknown entry 'temperature'", "signal 'detector_distance' is not configured"]:
        assert expected in text, expected


def test_dummy_config_has_valid_layout():
    cfg = load_config(ROOT / "configs" / "dummy.yaml")
    assert cfg.data["frames"]["path"] == "entry/data/data"
    assert cfg.data["geometry"]["energy"]["from"] == "event"


def test_bad_layout_is_a_config_error():
    raw = {"beamline": "x", "signals": {
        "scan_state": {"pv": "S", "kind": "busy"}, "scan_number": {"pv": "N"},
        "file_path": {"pv": "F"}}, "data": {"frames": {}}}
    with pytest.raises(ConfigError, match="data.frames"):
        load_config(raw)


# -- reading ------------------------------------------------------------------
@pytest.mark.parametrize("positions", [
    {"path": "d/pos", "unit": "um", "order": "yx"},
    {"path": "d/pos_xy_nm", "unit": "nm", "order": "xy"},
    {"path": "d/pos_T", "unit": "um", "order": "yx"},
    {"y": "s/y_mm", "x": "s/x_mm", "unit": "mm"},
])
def test_positions_all_conventions_give_yx_um(h5, positions):
    scan = read_scan(h5, layout(positions=positions), META)
    np.testing.assert_allclose(scan.positions_um, POS_UM)


def test_flip(h5):
    scan = read_scan(h5, layout(positions={"path": "d/pos", "flip": ["x"]}), META)
    np.testing.assert_allclose(scan.positions_um[:, 1], -POS_UM[:, 1])
    np.testing.assert_allclose(scan.positions_um[:, 0], POS_UM[:, 0])


def test_geometry_from_file_with_units_and_consistency_check(h5):
    geo = {"energy": {"from": "event", "path": "inst/energy_keV", "unit": "keV"},
           "detector_distance": {"from": "file", "path": "inst", "attr": "distance_mm",
                                 "unit": "mm"},
           "pixel_size": {"from": "file", "path": "inst", "attr": "pixel_um"}}
    scan = read_scan(h5, layout(geometry=geo), META)
    assert scan.energy_eV == 700.0 and scan.sources["energy"] == "event"
    assert scan.detector_distance_m == pytest.approx(0.25)
    assert scan.pixel_size_um == 75.0
    assert scan.warnings == []                         # 700 vs 705 eV: 0.7 %, within 1 %

    scan = read_scan(h5, layout(geometry=geo), dict(META, energy=750.0))
    assert any("energy: PV 750, file 705" in w for w in scan.warnings)


def test_event_value_missing(h5):
    geo = {"energy": {"from": "event", "path": "inst/energy_keV", "unit": "keV"}}
    scan = read_scan(h5, layout(geometry=geo), {"detector_distance": 0.25, "pixel_size": 75.0})
    assert scan.energy_eV == pytest.approx(705.0)      # fell back to the file ...
    assert "no PV value" in scan.warnings[0]           # ... and said so
    with pytest.raises(KeyError, match="energy"):
        read_scan(h5, layout(), {"detector_distance": 0.25, "pixel_size": 75.0})


def test_dx_and_shape_only(h5):
    scan = read_scan(h5, layout(), META, frames=False)
    assert scan.frames is None and scan.frames_shape == (3, 8, 8)
    expected = 1239.84 / 700.0 * 1e-3 * 0.25e6 / (8 * 75.0)         # lambda z / (N p), um
    assert scan.dx_um == pytest.approx(expected)


def test_missing_dataset_names_the_config_entry(h5):
    with pytest.raises(KeyError, match="data.positions.path: 'nope'"):
        read_scan(h5, layout(positions={"path": "nope"}), META)


def test_load_scan_from_job_and_event(h5):
    event = {"file_path": str(h5), "metadata": META, "data_layout": layout()}
    assert load_scan({"event": event}).frames.shape == (3, 8, 8)       # job.json
    assert load_scan(event).frames.dtype == np.float32                 # event itself
    with pytest.raises(ValueError, match="no data layout"):
        load_scan({"event": dict(event, data_layout=None)})


# -- integration ----------------------------------------------------------------
def test_tracker_puts_layout_into_events():
    lay = layout()
    t = ScanTracker("bl", {"file_timeout_s": 1}, has_writing=False,
                    file_probe=lambda p: (False, 0), data_layout=lay)
    t.feed(Obs("state", 0, "running", snap={"scan_number": 1, "file_path": "/f.h5"}))
    events = t.feed(Obs("state", 1, "aborted", snap={"scan_number": 1}))
    assert events[0].data_layout == lay
    assert type(events[0]).from_dict(events[0].to_dict()).data_layout == lay   # survives JSON


def test_check_data_cli(tmp_path, capsys):
    import subprocess
    import sys
    subprocess.run([sys.executable, str(ROOT / "sim" / "simulate_data.py"), "--out",
                    str(tmp_path), "--probe-sizes", "1.0"], check=True, capture_output=True)
    f = str(tmp_path / "sim_probe_1.00um.h5")
    assert cli_main(["check-data", str(ROOT / "configs" / "dummy.yaml"), f, "--offline"]) == 0
    out = capsys.readouterr().out
    assert "200 x 128 x 128" in out and "46.12 nm" in out and "mapping ok" in out

    bad = tmp_path / "bad.yaml"
    bad.write_text((ROOT / "configs" / "dummy.yaml").read_text()
                   .replace("entry/data/positions_um", "entry/data/pos"))
    assert cli_main(["check-data", str(bad), f, "--offline"]) == 1
    out = capsys.readouterr().out
    assert "data.positions.path: 'entry/data/pos' not found" in out
    assert "entry/data/positions_um" in out                         # the tree helps
