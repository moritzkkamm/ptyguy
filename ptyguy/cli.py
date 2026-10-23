"""Command line: python -m ptyguy <command> ...

    python -m ptyguy check  configs/dummy.yaml                 # are all signals reachable?
    python -m ptyguy listen configs/dummy.yaml --spool spool   # scan events -> spool/incoming
    python -m ptyguy launch examples/launcher.yaml             # spool -> reconstruction jobs
    python -m ptyguy check-data configs/dummy.yaml scan.h5     # does the data mapping fit?
"""
import argparse
import logging
import os
import time

from .signals import CATALOG, ConfigError, SignalMap, load_config


def _load(path):
    """Config or None (problem already printed)."""
    try:
        return load_config(path)
    except FileNotFoundError:
        print("config file not found: %s (current folder: %s)" % (path, os.getcwd()))
    except ConfigError as e:
        print(e)
    return None


def cmd_check(args):
    config = _load(args.config)
    if config is None:
        return 2

    sm = SignalMap(config)
    print("beamline: %s (%s)  config: %s" % (config.beamline, config.control_system,
                                              args.config))
    sm.connect(timeout=args.timeout)
    rows = sm.check()
    width = max(len(name) for name, *_ in rows)
    for name, required, ok, text in rows:
        mark = "[ok]" if ok else ("[!!]" if required else "[--]")
        print("  %s %-*s  %s" % (mark, width, name, text))

    not_configured = [n for n in CATALOG if n not in config.signals]
    if not_configured:
        print("  not configured (optional): %s" % ", ".join(not_configured))

    missing = [name for name, required, ok, _ in rows if required and not ok]
    sm.close()
    if missing:
        print("\nrequired signals missing, PtyGuy cannot run here: %s" % ", ".join(missing))
        return 1
    print("\nall required signals ok")
    return 0


def cmd_listen(args):
    from .listener import ListenerService, SpoolSink
    from .spool import Spool

    config = _load(args.config)
    if config is None:
        return 2
    sm = SignalMap(config)
    sm.connect(timeout=args.timeout)
    missing = sm.missing_required()
    if missing:
        print("required signals not reachable: %s (try: ptyguy check %s)"
              % (", ".join(missing), args.config))
        sm.close()
        return 1

    spool = Spool(args.spool)
    service = ListenerService(sm, SpoolSink(spool))
    print("beamline %s, writing events to %s" % (config.beamline, spool.root / "incoming"))
    print("Ctrl+C to stop")
    service.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        sm.close()
    return 0


def cmd_launch(args):
    from .launcher import LauncherService, load_launcher_config

    try:
        config = load_launcher_config(args.config)
    except FileNotFoundError:
        print("config file not found: %s (current folder: %s)" % (args.config, os.getcwd()))
        return 2
    except ConfigError as e:
        print(e)
        return 2
    service = LauncherService(config)
    print("Ctrl+C to stop (running jobs keep running)")
    try:
        service.run_forever(poll_s=args.poll)
    except KeyboardInterrupt:
        pass
    return 0


def _h5_tree(path, limit=60):
    """Every dataset and attribute in an HDF5 file, to help write the mapping."""
    import h5py
    lines = []

    def visit(name, obj):
        if isinstance(obj, h5py.Dataset):
            lines.append("  %-45s dataset %s %s" % (name, obj.shape, obj.dtype))
        for key, value in obj.attrs.items():
            lines.append("  %-45s attr    %s = %s" % (name, key, value))

    with h5py.File(path, "r") as f:
        f.visititems(visit)
    if len(lines) > limit:
        lines = lines[:limit] + ["  ... (%d more)" % (len(lines) - limit)]
    return "\n".join(lines)


def _nearest_neighbour_um(positions, max_points=2000):
    import numpy as np
    p = positions[:max_points]
    if len(p) < 2:
        return float("nan")
    d = np.sqrt(((p[:, None, :] - p[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(d, np.inf)
    return float(np.median(d.min(axis=1)))


def cmd_check_data(args):
    import h5py
    import numpy as np
    from .data import read_scan
    from .data.layout import GEOMETRY

    config = _load(args.config)
    if config is None:
        return 2
    if config.data is None:
        print("%s has no 'data:' section. What is in the file:" % args.config)
        print(_h5_tree(args.file))
        return 2

    # PV values for geometry 'from: event' (and for comparing with the file)
    metadata = {}
    if not args.offline:
        sm = SignalMap(config)
        sm.connect(timeout=args.timeout)
        for name in GEOMETRY + ("probe_size",):
            if sm.has(name) and not sm.status.get(name):
                metadata[name] = sm.read(name)
        sm.close()
    if args.offline or not metadata:
        print("(no PV values: geometry 'from: event' falls back to the file where possible)")

    try:
        scan = read_scan(args.file, config.data, metadata, frames=False)
    except (KeyError, ValueError, OSError) as e:
        print("cannot read %s with this mapping:\n  %s\n\nWhat is in the file:" % (args.file, e))
        print(_h5_tree(args.file))
        return 1

    with h5py.File(args.file, "r") as f:
        ds = f[config.data["frames"]["path"]]
        first = np.asarray(ds[0], dtype=np.float64)
        dtype = ds.dtype
    n, ny, nx = scan.frames_shape
    pos = scan.positions_um
    dx = scan.dx_um if not isinstance(scan.dx_um, tuple) else scan.dx_um[1]
    span_um = pos.max(axis=0) - pos.min(axis=0)
    step = _nearest_neighbour_um(pos)

    print("file        %s" % args.file)
    print("frames      %d x %d x %d  (%s in file)   frame 0: max %.3g, sum %.3g"
          % (n, ny, nx, dtype, first.max(), first.sum()))
    print("positions   %d points, y %.3g .. %.3g um, x %.3g .. %.3g um"
          % (len(pos), pos[:, 0].min(), pos[:, 0].max(), pos[:, 1].min(), pos[:, 1].max()))
    print("            median step %.3g um (nearest neighbour)" % step)
    units = {"energy": "eV", "detector_distance": "m", "pixel_size": "um"}
    values = {"energy": scan.energy_eV, "detector_distance": scan.detector_distance_m,
              "pixel_size": scan.pixel_size_um}
    for name in GEOMETRY:
        print("%-11s %.6g %s   (from %s)" % (name.replace("detector_", ""), values[name],
                                              units[name], scan.sources[name]))
    print("dx          %.4g nm  -> frame covers %.3g um, scan spans %.0f x %.0f px"
          % (dx * 1e3, nx * dx, span_um[0] / dx, span_um[1] / dx))
    probe = metadata.get("probe_size")
    if probe and step == step:
        print("overlap     probe %.3g um, step %.3g um -> linear overlap %.0f %%  "
              "(>~60 %% is comfortable)" % (probe, step, 100 * (1 - step / probe)))
    warnings = scan.warnings
    if not metadata:                       # already said above: no PVs, file used
        warnings = [w for w in warnings if "no PV value" not in w]
    for w in warnings:
        print("WARNING     %s" % w)
    print("\nmapping ok" + (" (with warnings)" if warnings else ""))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ptyguy")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="connect to every signal in a beamline config")
    c.add_argument("config")
    c.add_argument("--timeout", type=float, default=3.0, help="connect timeout, s")
    c.set_defaults(func=cmd_check, log_level=logging.CRITICAL)

    c = sub.add_parser("listen", help="watch the beamline, write one event per scan")
    c.add_argument("config")
    c.add_argument("--spool", default="spool", help="spool folder (default: ./spool)")
    c.add_argument("--timeout", type=float, default=5.0, help="connect timeout, s")
    c.set_defaults(func=cmd_listen, log_level=logging.INFO)

    c = sub.add_parser("launch", help="turn spooled scan events into reconstruction jobs")
    c.add_argument("config", help="launcher.yaml")
    c.add_argument("--poll", type=float, default=2.0, help="seconds between spool checks")
    c.set_defaults(func=cmd_launch, log_level=logging.INFO)

    c = sub.add_parser("check-data", help="read a scan file through the data mapping")
    c.add_argument("config", help="beamline config with a data: section")
    c.add_argument("file", help="a scan file (HDF5)")
    c.add_argument("--offline", action="store_true", help="don't read PV values")
    c.add_argument("--timeout", type=float, default=2.0, help="PV connect timeout, s")
    c.set_defaults(func=cmd_check_data, log_level=logging.CRITICAL)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else args.log_level,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("caproto").setLevel(logging.CRITICAL)   # we report connections
    return args.func(args)
