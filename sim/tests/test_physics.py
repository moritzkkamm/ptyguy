"""Pure-function tests: no EPICS needed."""
import json

import pytest

from dummy_beamline import (choose_dataset, focal_length_mm, load_datasets,
                            probe_size_um, wavelength_nm)


def test_wavelength_soft_xray():
    assert wavelength_nm(700) == pytest.approx(1.7712, rel=1e-3)


def test_focal_length_scales_with_energy():
    # f = D*dr/lambda: 150 um * 30 nm / 1.771 nm = 2.54 mm at 700 eV, f ~ E
    assert focal_length_mm(700) == pytest.approx(2.541, rel=1e-3)
    assert focal_length_mm(1400) == pytest.approx(2 * focal_length_mm(700))


def test_probe_size_in_and_out_of_focus():
    f = focal_length_mm(700)
    assert probe_size_um(f, 700) == pytest.approx(1.22 * 0.030)       # focal spot
    assert probe_size_um(f + 0.05, 700) == pytest.approx(
        150 * 0.05 / f, rel=1e-2)                                      # geometric cone
    assert probe_size_um(f - 0.05, 700) == pytest.approx(probe_size_um(f + 0.05, 700))


def test_energy_change_changes_probe_at_fixed_z():
    z = focal_length_mm(700) + 0.05
    assert probe_size_um(z, 700) != pytest.approx(probe_size_um(z, 750))


def test_choose_and_load_datasets(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"datasets": [
        {"probe_size_um": 0.3, "file": "a.h5"}, {"probe_size_um": 2.0, "file": "b.h5"}]}))
    ds = load_datasets(tmp_path)
    assert choose_dataset(0.5, ds).name == "a.h5"
    assert choose_dataset(1.5, ds).name == "b.h5"
