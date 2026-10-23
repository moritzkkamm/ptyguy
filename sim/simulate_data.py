"""Simulate ptychography datasets once, offline. No EPICS involved.

Writes one HDF5 file per probe size plus a manifest.json that the dummy
beamline reads at startup:

    python simulate_data.py --out data            # defaults: 0.3, 1.0, 2.0 um
    python simulate_data.py --out data --probe-sizes 0.5 1.5

HDF5 layout (one file per probe size):
    /entry/data/data            (N, H, W) uint32   diffraction patterns (photon counts)
    /entry/data/positions_um    (N, 2)    float64  scan positions (y, x)
    /entry/instrument           attrs: energy_eV, detector_distance_m,
                                       pixel_size_um, probe_size_um
    /entry/ground_truth/object  complex64          true object (for checking recons)
    /entry/ground_truth/probe   complex64          true probe
"""
import argparse
import json
from pathlib import Path

import h5py
import numpy as np

HC_EV_NM = 1239.84

ENERGY_EV = 700.0
DETECTOR_DISTANCE_M = 0.25
PIXEL_SIZE_UM = 75.0
FRAME = 128                 # detector frame size (pixels), cropped
N_POINTS = 200
PHOTONS_PER_FRAME = 1e6

def sample_pixel_size_um():
    wavelength_m = HC_EV_NM / ENERGY_EV * 1e-9
    return wavelength_m * DETECTOR_DISTANCE_M / (FRAME * PIXEL_SIZE_UM * 1e-6) * 1e6

def smooth_noise(shape, sigma_px, rng):
    ky = np.fft.fftfreq(shape[0])[:, None]
    kx = np.fft.fftfreq(shape[1])[None, :]
    kernel = np.exp(-2 * (np.pi * sigma_px) ** 2 * (kx ** 2 + ky ** 2))
    field = np.fft.ifft2(np.fft.fft2(rng.standard_normal(shape)) * kernel).real
    return field / field.std()

def make_object(shape, rng):
    domains = smooth_noise(shape, sigma_px=6, rng=rng) > 0
    phase = np.where(domains, 0.6, -0.6)
    amplitude = np.where(domains, 0.9, 1.0)
    return (amplitude * np.exp(1j * phase)).astype(np.complex64)

def make_probe(probe_size_um, dx_um):
    y, x = np.mgrid[-FRAME // 2:FRAME // 2, -FRAME // 2:FRAME // 2] * dx_um
    r = np.hypot(x, y)
    radius = probe_size_um / 2
    amplitude = 1 / (1 + np.exp((r - radius) / (0.1 * radius + dx_um)))
    phase = 2.0 * (r / max(radius, dx_um)) ** 2
    probe = amplitude * np.exp(1j * phase)
    return (probe / np.sqrt((np.abs(probe) ** 2).sum())).astype(np.complex64)

def fermat_spiral(n, step_um):
    k = np.arange(n)
    golden = np.pi * (3 - np.sqrt(5))
    r = step_um * np.sqrt(k) * 0.57
    return np.stack([r * np.sin(k*golden), r * np.cos(k*golden)], axis=1)

def simulate(probe_size_um, out_dir, rng):
    dx = sample_pixel_size_um()
    step_um = 0.4 * probe_size_um                   # ~60 % overlap
    positions_um = fermat_spiral(N_POINTS, step_um)
    positions_px = np.round(positions_um / dx).astype(int)
    
    span = positions_px.max(axis=0) - positions_px.min(axis=0)
    obj = make_object(tuple(span + FRAME + 8), rng)
    origin = -positions_px.min(axis=0) + 4
    probe = make_probe(probe_size_um, dx)
    
    frames = np.empty((N_POINTS, FRAME, FRAME), dtype=np.uint32)
    for i, (py, px) in enumerate(positions_px + origin):
        exit_wave = probe * obj[py:py + FRAME, px:px + FRAME]
        intensity = np.abs(np.fft.fftshift(np.fft.fft2(exit_wave, norm="ortho"))) ** 2
        frames[i] = rng.poisson(intensity / intensity.sum() * PHOTONS_PER_FRAME)
        
    name = "sim_probe_%.2fum.h5" % probe_size_um
    with h5py.File(out_dir / name, "w") as f:
        f.create_dataset("/entry/data/data", data=frames, compression="gzip")
        f["entry/data/positions_um"] = positions_um
        inst = f.create_group("entry/instrument")
        inst.attrs.update(energy_eV=ENERGY_EV, detector_distance_m=DETECTOR_DISTANCE_M,
                          pixel_size_um=PIXEL_SIZE_UM, probe_size_um=probe_size_um)
        f["entry/ground_truth/object"] = obj
        f["entry/ground_truth/probe"] = probe
        
    return {"probe_size_um": probe_size_um, "file": name, "n_points": N_POINTS,
            "energy_eV": ENERGY_EV, "step_um": step_um}
    
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=Path("data"))
    p.add_argument("--probe-sizes", type=float, nargs="+", default=[0.3, 1.0, 2.0])
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    
    a.out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(a.seed)
    entries = []
    for size in a.probe_sizes:
        entries.append(simulate(size, a.out, rng))
        print("wrote", entries[-1]["file"])
        
    manifest = {"sample_pixel_size_um": sample_pixel_size_um(), "datasets": entries}
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("wrote manifest.json")
    
if __name__=="__main__":
    main()