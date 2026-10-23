"""Dummy ptychography beamline as an EPICS IOC (caproto).

    python simulate_data.py --out data                          # once
    python dummy_beamline.py --datasets data --data-root scans --interfaces 127.0.0.1

PVs (prefix DUMMY:):
    OPT:   ENERGY, ZP:Z, ZP:Z_RBV, ZP:MOVING, ZP:SPEED, PROBE:SIZE
    SCAN:  START, ABORT, STATE, NUMBER, NPOINTS, PROGRESS, TYPE, STEP, DURATION
    DET:   FILEPATH, WRITING, FLUSH_DELAY, DISTANCE, PIXEL_SIZE

Rules:
    - every PV is written only by the group that owns it
    - groups talk to each other through methods, not by writing PVs
    - scan done (SCAN:STATE = DONE) is NOT file ready (DET:WRITING = 0)
"""
# ============================================================================
# 1. Imports
# ============================================================================
import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from caproto import ChannelType
from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run

log = logging.getLogger("dummy")

# ============================================================================
# 2. Constants
# ============================================================================
HC_EV_NM = 1239.84              # h*c in eV*nm: lambda[nm] = 1239.84 / E[eV]
ZP_DIAMETER_UM = 150.0          # zone plate diameter D
ZP_OUTER_ZONE_NM = 30.0         # outermost zone width dr
STATES = ["IDLE", "RUNNING", "DONE", "ABORTED"]
SCAN_TYPES = ["FERMAT", "RASTER"]
DEFAULT_ENERGY_EV = 700.0
DEFOCUS_MM = 0.05               # start 50 um out of focus


# ============================================================================
# 3. Pure functions (no EPICS, test these with pytest)
# ============================================================================
def wavelength_nm(energy_eV):
    return HC_EV_NM / energy_eV


def focal_length_mm(energy_eV):
    """f = D * dr / lambda. Units: um * nm / nm = um, then -> mm."""
    return ZP_DIAMETER_UM * ZP_OUTER_ZONE_NM / wavelength_nm(energy_eV) * 1e-3


def probe_size_um(z_mm, energy_eV):
    """Beam diameter at the sample for zone-plate-to-sample distance z.

    Geometric cone: D * |z - f| / f, floored at the focal spot 1.22 * dr.
    Added in quadrature so the transition through focus is smooth.
    """
    f = focal_length_mm(energy_eV)
    geometric = ZP_DIAMETER_UM * abs(z_mm - f) / f
    focus = 1.22 * ZP_OUTER_ZONE_NM * 1e-3
    return (geometric ** 2 + focus ** 2) ** 0.5


def load_datasets(folder):
    """Read manifest.json written by simulate_data.py -> {probe_size_um: Path}."""
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    return {d["probe_size_um"]: folder / d["file"] for d in manifest["datasets"]}


def choose_dataset(probe_um, datasets):
    """Pre-simulated dataset whose probe size is closest to probe_um."""
    best = min(datasets, key=lambda size: abs(size - probe_um))
    return datasets[best]

def write_empty(dst):
    """Replace dst by an empty file. Unlink first: dst may be a hardlink to a
    dataset (same scan number after an IOC restart), and writing through it
    would truncate the dataset itself."""
    dst = Path(dst)
    if dst.exists():
        dst.unlink()
    dst.write_bytes(b"")

def link_or_copy(src, dst):
    """Hardlink src to dst (no extra disk, works on Windows without admin).

    Hardlinks only work within one drive/filesystem, so fall back to a copy.
    """
    dst = Path(dst)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        log.warning("hardlink failed (different drive?), copying %s", src)
        shutil.copyfile(src, dst)


_DEFAULT_Z_MM = focal_length_mm(DEFAULT_ENERGY_EV) + DEFOCUS_MM


# ============================================================================
# 4. Data class: optics snapshot taken when a scan starts
# ============================================================================
@dataclass
class ScanSnapshot:
    number: int
    energy_eV: float
    zp_z_mm: float
    probe_size_um: float
    npoints: int
    scan_type: str
    step_um: float
    started: float


# ============================================================================
# 5. PV groups
# ============================================================================
class Optics(PVGroup):
    """What the beam looks like: energy, zone plate position, probe size."""

    energy = pvproperty(name="ENERGY", value=DEFAULT_ENERGY_EV, units="eV",
                        precision=1, lower_ctrl_limit=100.0, upper_ctrl_limit=2000.0)
    zp_z = pvproperty(name="ZP:Z", value=_DEFAULT_Z_MM, units="mm", precision=4,
                      lower_ctrl_limit=0.1, upper_ctrl_limit=20.0)
    zp_z_rbv = pvproperty(name="ZP:Z_RBV", value=_DEFAULT_Z_MM, units="mm",
                          precision=4, read_only=True)
    zp_moving = pvproperty(name="ZP:MOVING", value=0, read_only=True)
    zp_speed = pvproperty(name="ZP:SPEED", value=0.5, units="mm/s", precision=3)
    probe_size = pvproperty(name="PROBE:SIZE", units="um", precision=3, read_only=True,
                            value=probe_size_um(_DEFAULT_Z_MM, DEFAULT_ENERGY_EV))

    _move_task = None

    @energy.putter
    async def energy(self, instance, value):
        # Inside a putter the PV still holds the OLD value, so pass the new one.
        await self._update_probe(energy_eV=value)
        return value

    @zp_z.putter
    async def zp_z(self, instance, value):
        if self._move_task is not None and not self._move_task.done():
            self._move_task.cancel()                 # new setpoint replaces old move
        self._move_task = asyncio.create_task(self._move(value))
        return value

    async def _move(self, target_mm):
        dt = 0.1                                     # readback update interval, s
        await self.zp_moving.write(1)
        try:
            pos = self.zp_z_rbv.value
            while abs(target_mm - pos) > 1e-9:
                max_step = self.zp_speed.value * dt
                pos += max(-max_step, min(max_step, target_mm - pos))
                await self.zp_z_rbv.write(pos)
                await self._update_probe(z_mm=pos)
                await asyncio.sleep(dt)
        finally:                                     # also runs when cancelled
            await self.zp_moving.write(0)

    async def _update_probe(self, energy_eV=None, z_mm=None):
        e = self.energy.value if energy_eV is None else energy_eV
        z = self.zp_z_rbv.value if z_mm is None else z_mm
        await self.probe_size.write(probe_size_um(z, e))

    def current(self):
        """(energy_eV, zp_z_mm, probe_size_um), read from readbacks."""
        return self.energy.value, self.zp_z_rbv.value, self.probe_size.value


class Detector(PVGroup):
    """Where the data goes: file path, writing flag, flush delay."""

    # Long strings: a plain char waveform (bytes), like areaDetector's FullFileName_RBV.
    # Clients decode it: caproto get -S, pyepics PV.get(as_string=True).
    filepath = pvproperty(name="FILEPATH", value="", max_length=256,
                          dtype=ChannelType.CHAR, read_only=True)
    writing = pvproperty(name="WRITING", value=0, read_only=True)
    flush_delay = pvproperty(name="FLUSH_DELAY", value=3.0, units="s", precision=1,
                             lower_ctrl_limit=0.0, upper_ctrl_limit=600.0)
    distance = pvproperty(name="DISTANCE", value=0.25, units="m", precision=4)
    pixel_size = pvproperty(name="PIXEL_SIZE", value=75.0, units="um", precision=1,
                            read_only=True)

    _flush_task = None
    _pending_dataset = None
    _path = None                    # the Python-side truth; FILEPATH only publishes it

    async def open_file(self, snap):
        path = Path(self.parent.data_root) / ("scan_%05d.h5" % snap.number)
        self._pending_dataset = choose_dataset(snap.probe_size_um, self.parent.datasets)
        self._path = path
        await self.filepath.write(str(path))
        await self.writing.write(1)
        log.info("scan %d: writing %s (dataset %s)", snap.number, path.name,
                 self._pending_dataset.name)

    async def close_file(self, complete):
        if complete:
            # Return immediately; the file becomes ready FLUSH_DELAY seconds later.
            self._flush_task = asyncio.create_task(self._flush())
        else:
            write_empty(self._path)                      # aborted: empty/faulty file
            await self.writing.write(0)
            log.info("aborted: wrote empty %s", self._path.name)

    async def _flush(self):
        await asyncio.sleep(self.flush_delay.value)
        link_or_copy(self._pending_dataset, self._path)
        await self.writing.write(0)
        log.info("file ready: %s", self._path.name)

    def is_writing(self):
        return self.writing.value == 1


class Scan(PVGroup):
    """What is being measured: state machine, counter, progress."""

    start = pvproperty(name="START", value=0)
    abort = pvproperty(name="ABORT", value=0)
    state = pvproperty(name="STATE", value="IDLE", enum_strings=STATES,
                       dtype=ChannelType.ENUM, read_only=True)
    number = pvproperty(name="NUMBER", value=0, read_only=True)
    npoints = pvproperty(name="NPOINTS", value=200, lower_ctrl_limit=1,
                         upper_ctrl_limit=10000)
    progress = pvproperty(name="PROGRESS", value=0, read_only=True)
    scan_type = pvproperty(name="TYPE", value="FERMAT", enum_strings=SCAN_TYPES,
                           dtype=ChannelType.ENUM)
    step = pvproperty(name="STEP", value=0.1, units="um", precision=3)
    duration = pvproperty(name="DURATION", value=10.0, units="s", precision=1,
                          lower_ctrl_limit=0.1, upper_ctrl_limit=3600.0)

    _run_task = None

    @start.putter
    async def start(self, instance, value):
        if value != 1:
            return 0
        if self.state.value == "RUNNING":
            log.warning("START refused: scan already running")
        elif self.parent.det.is_writing():
            log.warning("START refused: detector still writing previous file")
        else:
            self._run_task = asyncio.create_task(self._run())
        return 0                                     # START always reads back 0

    @abort.putter
    async def abort(self, instance, value):
        if value == 1 and self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        return 0

    async def _run(self):
        det = self.parent.det
        energy, z, probe = self.parent.optics.current()
        snap = ScanSnapshot(number=self.number.value + 1, energy_eV=energy, zp_z_mm=z,
                            probe_size_um=probe, npoints=self.npoints.value,
                            scan_type=self.scan_type.value, step_um=self.step.value,
                            started=time.time())
        # Metadata first, state last: a listener reacting to STATE sees fresh values.
        await self.number.write(snap.number)
        await self.progress.write(0)
        await det.open_file(snap)
        await self.state.write("RUNNING")
        log.info("scan %d started (probe %.3f um, %d points)", snap.number,
                 snap.probe_size_um, snap.npoints)
        try:
            dt = self.duration.value / snap.npoints
            for i in range(snap.npoints):
                await asyncio.sleep(dt)
                await self.progress.write(i + 1)
        except asyncio.CancelledError:
            await self.state.write("ABORTED")
            await det.close_file(complete=False)
            log.info("scan %d aborted at point %d", snap.number, self.progress.value)
            raise
        await self.state.write("DONE")
        await det.close_file(complete=True)
        log.info("scan %d done, detector flushing", snap.number)


class DummyBeamline(PVGroup):
    """Assembles the groups. No logic of its own."""

    optics = SubGroup(Optics, prefix="OPT:")
    scan = SubGroup(Scan, prefix="SCAN:")
    det = SubGroup(Detector, prefix="DET:")

    def __init__(self, *args, data_root, datasets_dir, **kwargs):
        # Set before super().__init__, which creates the subgroups.
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.datasets = load_datasets(datasets_dir)
        super().__init__(*args, **kwargs)


# ============================================================================
# 6. main
# ============================================================================
def main():
    # Our own arguments first; everything else goes to caproto's parser.
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--data-root", default="scans", help="where scan files appear")
    p.add_argument("--datasets", default="data", help="folder with manifest.json")
    own, rest = p.parse_known_args()
    sys.argv = sys.argv[:1] + rest      # caproto's parser reads sys.argv itself

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="DUMMY:", desc="PtyGuy dummy beamline",
        supported_async_libs=["asyncio"])
    ioc = DummyBeamline(data_root=own.data_root, datasets_dir=own.datasets, **ioc_options)
    log.info("datasets: %s", ", ".join("%.2f um" % s for s in sorted(ioc.datasets)))
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()