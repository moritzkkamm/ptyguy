"""Read a scan file through the data layout. For reconstruction scripts.

    from ptyguy.data import load_scan
    scan = load_scan(job)          # job.json dict (or a ScanEvent / event dict)
    scan.frames                    # (N, H, W) float32
    scan.positions_um              # (N, 2): always (y, x), in um, flips applied
    scan.energy_eV, scan.detector_distance_m, scan.pixel_size_um
    scan.dx_um                     # real-space pixel size lambda*z/(N*p)
    scan.warnings                  # e.g. PV and file disagree on the energy

Raw data is opened read-only, always.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

import h5py
import numpy as np

from ..signals.catalog import CATALOG, convert

HC_EV_NM = 1239.84
AGREE = 0.01                          # PV and file may differ by 1 % before we warn


@dataclass
class Scan:
    file_path: str
    frames: Any                       # (N, H, W) float32, or None if read with frames=False
    frames_shape: tuple
    positions_um: np.ndarray
    energy_eV: float
    detector_distance_m: float
    pixel_size_um: float
    sources: Dict[str, str] = field(default_factory=dict)     # where each geometry value came from
    warnings: List[str] = field(default_factory=list)

    @property
    def wavelength_um(self):
        return HC_EV_NM / self.energy_eV * 1e-3

    @property
    def dx_yx_um(self):
        """Real-space pixel size per axis: lambda * z / (N * p)."""
        ny, nx = self.frames_shape[-2:]
        lz = self.wavelength_um * self.detector_distance_m * 1e6
        return lz / (ny * self.pixel_size_um), lz / (nx * self.pixel_size_um)

    @property
    def dx_um(self):
        dy, dx = self.dx_yx_um
        return dx if np.isclose(dy, dx) else (dy, dx)


def _event_dict(source):
    """job dict -> its event; ScanEvent -> dict; event dict -> itself."""
    if hasattr(source, "to_dict"):
        return source.to_dict()
    return source.get("event", source)


def _get(f, path, where):
    """f[path] with an error that names the config entry, not just the HDF5 path."""
    try:
        return f[path]
    except KeyError:
        raise KeyError("%s: '%s' not found in the file" % (where, path)) from None


def _read_value(f, item):
    """A scalar from a dataset or an attribute."""
    obj = f[item["path"]]
    value = obj.attrs[item["attr"]] if item.get("attr") else obj[()]
    return float(np.asarray(value).reshape(-1)[0])


def _read_positions(f, pos):
    if "path" in pos:
        p = np.asarray(_get(f, pos["path"], "data.positions.path")[()], dtype=np.float64)
        if p.ndim != 2 or 2 not in p.shape:
            raise ValueError("positions %s: expected shape (N, 2), got %s" % (pos["path"], p.shape))
        if p.shape[1] != 2:                      # stored as (2, N)
            p = p.T
        if pos["order"] == "xy":
            p = p[:, ::-1]
    else:
        y = np.asarray(_get(f, pos["y"], "data.positions.y")[()], dtype=np.float64).reshape(-1)
        x = np.asarray(_get(f, pos["x"], "data.positions.x")[()], dtype=np.float64).reshape(-1)
        if len(y) != len(x):
            raise ValueError("positions: %d y values but %d x values" % (len(y), len(x)))
        p = np.stack([y, x], axis=1)
    p = convert(p, pos["unit"], "um")
    for axis in pos["flip"]:
        p[:, {"y": 0, "x": 1}[axis]] *= -1
    return p


def read_scan(file_path, layout, metadata=None, frames=True):
    """Core reader. `metadata`: the event's metadata (PV values, canonical units)."""
    metadata = metadata or {}
    if layout is None:
        raise ValueError("no data layout: add a 'data:' section to the beamline config")
    warnings, sources, geometry = [], {}, {}
    with h5py.File(file_path, "r") as f:
        ds = _get(f, layout["frames"]["path"], "data.frames.path")
        shape = tuple(ds.shape)
        data = ds[()].astype(np.float32) if frames else None   # frames=False: shape only
        positions = _read_positions(f, layout["positions"])

        for name, item in layout["geometry"].items():
            canonical = CATALOG[name].unit
            pv = metadata.get(name)
            in_file = None
            if item.get("path"):
                try:
                    in_file = convert(_read_value(f, item), item["unit"], canonical)
                except (KeyError, ValueError) as e:
                    if item["from"] == "file":
                        raise KeyError("%s: not found in file (%s)" % (name, e)) from None
                    warnings.append("%s: file location given but unreadable (%s)" % (name, e))
            if item["from"] == "file":
                value, sources[name] = in_file, "file"
            else:
                if pv is None:
                    if in_file is None:
                        raise KeyError("%s: from event, but the event has no %s "
                                       "(and no file location to fall back on)" % (name, name))
                    value, sources[name] = in_file, "file (no PV value)"
                    warnings.append("%s: no PV value in the event, used the file" % name)
                else:
                    value, sources[name] = float(pv), "event"
            if pv is not None and in_file is not None:
                rel = abs(float(pv) - in_file) / max(abs(in_file), 1e-30)
                if rel > AGREE:
                    warnings.append("%s: PV %.6g, file %.6g %s (%.1f %% apart)"
                                    % (name, float(pv), in_file, canonical, 100 * rel))
            geometry[name] = value

    if len(positions) != shape[0]:
        warnings.append("%d frames but %d positions" % (shape[0], len(positions)))
    return Scan(file_path=str(file_path), frames=data, frames_shape=shape, positions_um=positions,
                energy_eV=geometry["energy"], detector_distance_m=geometry["detector_distance"],
                pixel_size_um=geometry["pixel_size"], sources=sources, warnings=warnings)


def load_scan(job_or_event, frames=True):
    """The usual entry point for reconstruction scripts: load_scan(job)."""
    event = _event_dict(job_or_event)
    layout = event.get("data_layout")
    if layout is None:
        raise ValueError("this event has no data layout: add a 'data:' section to the beamline "
                         "config (and re-run the listener), or read the file yourself")
    return read_scan(event["file_path"], layout, event.get("metadata"), frames=frames)
