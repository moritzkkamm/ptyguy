"""Config loading, validation and decoding: no control system needed."""
from pathlib import Path

import numpy as np
import pytest

from ptyguy.signals import ConfigError, load_config
from ptyguy.signals.catalog import convert

ROOT = Path(__file__).resolve().parent.parent


def minimal(**extra_signals):
    signals = {
        "scan_state": {"pv": "X:STATE", "states": {"running": 1, "done": [0, 3]}},
        "scan_number": {"pv": "X:NR"},
        "file_path": {"pv": "X:FILE", "type": "string"},
    }
    signals.update(extra_signals)
    return {"beamline": "test", "signals": signals}


def test_dummy_config_loads():
    cfg = load_config(ROOT / "configs" / "dummy.yaml")
    assert cfg.beamline == "dummy"
    assert cfg.signals["energy"].unit == "eV"
    assert cfg.options["file_timeout_s"] == 300


def test_unit_conversion():
    assert convert(7, "keV", "eV") == 7000
    assert convert(300, "nm", "um") == pytest.approx(0.3)
    assert convert(2, "µm", "um") == 2
    with pytest.raises(ValueError):
        convert(1, "eV", "m")


def test_all_problems_reported_at_once():
    raw = {"beamline": "bad", "control_system": "tango", "signals": {
        "scan_state": {"pv": "X", "kind": "busy", "states": {"done": 0}},
        "energy": {"pv": "E", "unit": "um"},
        "probe_size": {"pv": "P", "value": 1.0},
        "scan_numbr": {"pv": "N"},
    }, "options": {"retries": 3}}
    with pytest.raises(ConfigError) as e:
        load_config(raw)
    text = str(e.value)
    for expected in ["tango", "either 'states", "expected energy", "exactly one",
                     "unknown signal 'scan_numbr'", "required signal 'file_path'",
                     "unknown option 'retries'"]:
        assert expected in text, expected


def test_decode_state_values():
    s = load_config(minimal()).signals["scan_state"]
    assert s.decode(1) == "running"
    assert s.decode(0) == "done" and s.decode(3) == "done"   # several values per state
    assert s.decode(7) == "other"                            # unmapped
    assert s.decode(np.uint16(1)) == "running"              # numpy from the wire


def test_decode_state_labels():
    cfg = load_config(minimal(scan_state={"pv": "S", "states": {"done": "DONE",
                                                                 "running": "RUNNING"}}))
    s = cfg.signals["scan_state"]
    assert s.decode(b"DONE") == "done" and s.decode("RUNNING") == "running"


def test_decode_busy():
    s = load_config(minimal(scan_state={"pv": "B", "kind": "busy"})).signals["scan_state"]
    assert s.decode(1) == "running" and s.decode(0) == "idle"


def test_decode_char_waveform_and_units():
    cfg = load_config(minimal(energy={"pv": "E", "unit": "keV"},
                              probe_size={"value": 300, "unit": "nm"}))
    path = np.frombuffer(b"/data/scan_00001.h5\x00\x00", dtype=np.uint8)
    assert cfg.signals["file_path"].decode(path) == "/data/scan_00001.h5"
    assert cfg.signals["energy"].decode(0.7) == pytest.approx(700.0)   # keV -> eV
    assert cfg.signals["probe_size"].decode(300) == pytest.approx(0.3)  # constant, nm -> um
