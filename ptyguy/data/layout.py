"""The `data:` section of a beamline config: where things are in the scan files.

    data:
      format: hdf5
      frames:    {path: entry/data/data}                       # (N, H, W)
      positions: {path: entry/data/positions_um, unit: um, order: yx, flip: []}
      #   or two datasets:  {y: entry/sample/y, x: entry/sample/x, unit: mm}
      geometry:                                                # from: event (PV) | file
        energy:            {from: event, path: entry/instrument, attr: energy_eV, unit: eV}
        detector_distance: {from: event}
        pixel_size:        {from: file, path: entry/instrument, attr: pixel_size_um, unit: um}

Geometry: `from` says which value is USED. If a file location is given as well,
the other source is compared against it and a warning is raised if they differ.
Missing geometry entries default to {from: event}.

parse_data_layout() turns the raw YAML into a plain, JSON-serialisable dict:
it travels with every scan event, so a re-run months later can still read the file.
"""
from ..signals.catalog import CATALOG, UNITS, dimension

FORMATS = ("hdf5",)
GEOMETRY = ("energy", "detector_distance", "pixel_size")   # same names as the signals
_UNIT_NAMES = set(UNITS) | {"µm", "μm", "micron"}


def _check_unit(where, unit, canonical, problems):
    if unit not in _UNIT_NAMES:
        problems.append("%s: unknown unit %r (known: %s)" % (where, unit, ", ".join(UNITS)))
    elif dimension(unit) != dimension(canonical):
        problems.append("%s: unit %r is a %s, expected %s (like %r)"
                        % (where, unit, dimension(unit), dimension(canonical), canonical))


def parse_data_layout(raw, problems, signals=()):
    """Validate the `data:` section. Returns the normalised dict, or None if absent.

    `signals`: names of configured signals, to check that `from: event` is possible.
    Problems are appended to `problems` (the caller raises them all at once).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("data: expected a mapping, got %r" % (raw,))
        return None
    n_before = len(problems)
    unknown = set(raw) - {"format", "frames", "positions", "geometry"}
    if unknown:
        problems.append("data: unknown keys %s" % sorted(unknown))

    fmt = raw.get("format", "hdf5")
    if fmt not in FORMATS:
        problems.append("data.format %r not supported (have: %s)" % (fmt, ", ".join(FORMATS)))

    frames = raw.get("frames") or {}
    if not frames.get("path"):
        problems.append("data.frames: 'path' is missing (dataset with the diffraction frames)")

    pos = dict(raw.get("positions") or {})
    has_combined, has_split = "path" in pos, ("x" in pos or "y" in pos)
    if has_combined == has_split or (has_split and not ("x" in pos and "y" in pos)):
        problems.append("data.positions: give either 'path' (one (N,2) dataset) or both "
                        "'y' and 'x' (two datasets)")
    pos.setdefault("unit", "um")
    _check_unit("data.positions", pos["unit"], "um", problems)
    pos.setdefault("order", "yx")
    if pos["order"] not in ("yx", "xy"):
        problems.append("data.positions.order must be yx or xy, got %r" % pos["order"])
    flip = pos.get("flip") or []
    pos["flip"] = [flip] if isinstance(flip, str) else list(flip)
    if set(pos["flip"]) - {"x", "y"}:
        problems.append("data.positions.flip: only x and y allowed, got %s" % pos["flip"])
    unknown = set(pos) - {"path", "x", "y", "unit", "order", "flip"}
    if unknown:
        problems.append("data.positions: unknown keys %s" % sorted(unknown))

    geometry = {}
    raw_geo = raw.get("geometry") or {}
    for name in set(raw_geo) - set(GEOMETRY):
        problems.append("data.geometry: unknown entry %r (known: %s)" % (name, ", ".join(GEOMETRY)))
    for name in GEOMETRY:
        item = dict(raw_geo.get(name) or {})
        item.setdefault("from", "event")
        where = "data.geometry.%s" % name
        if item["from"] not in ("event", "file"):
            problems.append("%s: 'from' must be event or file, got %r" % (where, item["from"]))
        if item["from"] == "file" and not item.get("path"):
            problems.append("%s: from: file needs a 'path' (and 'attr' for an attribute)" % where)
        if item["from"] == "event" and signals and name not in signals:
            problems.append("%s: from: event, but signal %r is not configured" % (where, name))
        canonical = CATALOG[name].unit
        item.setdefault("unit", canonical)
        _check_unit(where, item["unit"], canonical, problems)
        unknown = set(item) - {"from", "path", "attr", "unit"}
        if unknown:
            problems.append("%s: unknown keys %s" % (where, sorted(unknown)))
        geometry[name] = item

    if len(problems) > n_before:
        return None
    return {"format": fmt, "frames": {"path": frames["path"]}, "positions": pos,
            "geometry": geometry}
