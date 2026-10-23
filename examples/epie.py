"""Toy ePIE reconstruction for the dummy beamline data, using the PtyGuy hook.

    python examples/epie.py path/to/job.json        (normally started by the launcher)

Recipe params (all optional):
    iterations      default 100
    alpha, beta     object / probe step size, default 1.0 / 1.0
    probe_update_from   first iteration that updates the probe, default 1
    backend         array module: numpy (default) or cupy
    seed            random position order, default 0

Physics (matching sim/simulate_data.py): far field, ortho FFT, data stored
fftshifted, pixel size at the sample dx = lambda * z / (N * p).
"""
import importlib
import sys
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run without install
from ptyguy.hook import Hook, load_job  # noqa: E402

HC_EV_NM = 1239.84
MARGIN = 4                                    # same as the simulation


def load_scan(path):
    with h5py.File(path, "r") as f:
        data = f["entry/data/data"][()].astype(np.float32)
        positions_um = f["entry/data/positions_um"][()]
        inst = dict(f["entry/instrument"].attrs)
        truth = f["entry/ground_truth/object"][()] if "entry/ground_truth/object" in f else None
    return data, positions_um, inst, truth


def initial_probe(job, shape, dx_um, total_intensity):
    """Library probe if the launcher picked one, else a flat disk of the nominal size."""
    path = job["probe"]["path"]
    if path:
        probe = np.load(path).astype(np.complex64)
        source = "library: " + Path(path).name
    else:
        size_um = job["event"]["metadata"].get("probe_size") or 1.0
        y, x = np.mgrid[-shape[0] // 2:shape[0] // 2, -shape[1] // 2:shape[1] // 2] * dx_um
        probe = (np.hypot(x, y) <= size_um / 2).astype(np.complex64)
        source = "flat disk %.2f um" % size_um
    # ortho FFT + |object| ~ 1: sum |P|^2 should match the mean photon count per frame
    probe *= np.sqrt(total_intensity / (np.abs(probe) ** 2).sum())
    return probe, source


def truth_error(obj, truth, coverage):
    """Rough sanity check only: relative error after removing a global complex factor."""
    if truth is None or truth.shape != obj.shape:
        return None
    mask = coverage > 0.1 * coverage.max()
    o, t = obj[mask], truth[mask]
    g = np.vdot(o, t) / np.vdot(o, o)        # best scale + global phase
    return float(np.linalg.norm(g * o - t) / np.linalg.norm(t))


def main(job_path=None):
    job = load_job(job_path)
    params = job.get("params", {})
    xp = importlib.import_module(params.get("backend", "numpy"))
    n_iter = int(params.get("iterations", 100))
    alpha, beta = float(params.get("alpha", 1.0)), float(params.get("beta", 1.0))
    probe_from = int(params.get("probe_update_from", 1))
    rng = np.random.default_rng(int(params.get("seed", 0)))

    data, positions_um, inst, truth = load_scan(job["event"]["file_path"])
    n_pos, ny, nx = data.shape
    wavelength_um = HC_EV_NM / inst["energy_eV"] * 1e-3
    dx_um = wavelength_um * inst["detector_distance_m"] * 1e6 / (nx * inst["pixel_size_um"])

    pos_px = np.round(positions_um / dx_um).astype(int)
    pos_px = pos_px - pos_px.min(axis=0) + MARGIN
    obj_shape = tuple(int(n) for n in pos_px.max(axis=0) + (ny, nx) + MARGIN)

    amplitudes = xp.asarray(np.sqrt(np.fft.ifftshift(data, axes=(-2, -1))))
    probe_np, probe_source = initial_probe(job, (ny, nx), dx_um, data.sum(axis=(1, 2)).mean())
    probe = xp.asarray(probe_np)
    obj = xp.ones(obj_shape, dtype=xp.complex64)
    norm = float(xp.sum(amplitudes ** 2))
    print("ePIE: %d positions, frame %dx%d, dx %.1f nm, object %s, probe %s"
          % (n_pos, ny, nx, dx_um * 1e3, obj_shape, probe_source), flush=True)

    hook = Hook(job, total=n_iter)
    for it in range(1, n_iter + 1):
        err = 0.0
        for j in rng.permutation(n_pos):
            py, px = pos_px[j]
            view = obj[py:py + ny, px:px + nx]
            exit_wave = probe * view
            far = xp.fft.fft2(exit_wave, norm="ortho")
            mod = xp.abs(far)
            err += float(xp.sum((mod - amplitudes[j]) ** 2))
            far = amplitudes[j] * far / xp.maximum(mod, 1e-12)   # modulus constraint
            diff = xp.fft.ifft2(far, norm="ortho") - exit_wave
            old_view = view.copy()
            obj[py:py + ny, px:px + nx] = view + alpha * xp.conj(probe) * diff / (
                xp.max(xp.abs(probe)) ** 2)
            if it >= probe_from:
                probe = probe + beta * xp.conj(old_view) * diff / xp.max(xp.abs(old_view)) ** 2
        err /= norm
        hook.update(it, err, obj=obj, probe=probe)
        if it == 1 or it % 10 == 0 or it == n_iter:
            print("iteration %4d  error %.4g" % (it, err), flush=True)

    obj_np = obj.get() if hasattr(obj, "get") else obj
    coverage = np.zeros(obj_shape)
    p_int = np.abs(probe.get() if hasattr(probe, "get") else probe) ** 2
    for py, px in pos_px:
        coverage[py:py + ny, px:px + nx] += p_int
    t_err = truth_error(obj_np, truth, coverage)
    extra = {"dx_um": dx_um, "probe_source": probe_source}
    if t_err is not None:
        extra["truth_error"] = t_err
        print("error vs ground truth (scale + global phase removed): %.3f" % t_err, flush=True)
    hook.finish(obj, probe, **extra)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
