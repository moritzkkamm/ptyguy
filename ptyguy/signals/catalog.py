"""The logical signals PtyGuy understands, and their canonical units.

Everything inside PtyGuy uses these names and units. Beamline-specific names
and units only exist in the config file and are translated at the edge.
"""
from dataclasses import dataclass
from typing import Optional

# What a scan can be doing, as PtyGuy sees it. Beamline values that a config
# doesn't map end up as "other" (e.g. a PAUSED state PtyGuy doesn't care about).
LOGICAL_STATES = ("idle", "running", "done", "aborted", "other")

@dataclass(frozen=True)
class SignalSpec:
    name: str
    required: bool
    kind: str                   # "state" | "int" | "float" | "str" | "bool"
    unit: Optional[str] = None  # canonical unit; None = unitless
    doc: str = ""
    
_SPECS = [
    SignalSpec("scan_state", True, "state", doc="tells PtyGuy when a scan ends"),
    SignalSpec("scan_number", True, "int", doc="identifies the scan"),
    SignalSpec("file_path", True, "str", doc="absolute path of the raw data file"),
    SignalSpec("writing", False, "bool", doc="true while the data file is still open"),
    SignalSpec("energy", False, "float", "eV"),
    SignalSpec("probe_size", False, "float", "um", doc="beam size at the sample"),
    SignalSpec("detector_distance", False, "float", "m"),
    SignalSpec("pixel_size", False, "float", "um", doc="detector pixel size"),
    SignalSpec("npoints", False, "int"),
    SignalSpec("scan_type", False, "str"),
    SignalSpec("step", False, "float", "um"),
]

CATALOG = {s.name: s for s in _SPECS}

# unit -> (dimension, factor to the dimension's base unit)
UNITS = {
    "eV": ("energy", 1.0),
    "keV": ("energy", 1e3),
    "m": ("length", 1.0),
    "mm": ("length", 1e-3),
    "um": ("length", 1e-6),
    "nm": ("length", 1e-9),
}

_ALIASES = {"µm": "um", "μm": "um", "micron": "um"}

def normalize_unit(unit):
    return _ALIASES.get(unit, unit)

def dimension(unit):
    return UNITS[normalize_unit(unit)][0]

def convert(value, from_unit, to_unit):
    """convert(7, "keV", "eV") -> 7000.0 Raises ValueError across dimensions."""
    (dim_from, f_from) = UNITS[normalize_unit(from_unit)]
    (dim_to, f_to) = UNITS[normalize_unit(to_unit)]
    if dim_from != dim_to:
        raise ValueError("cannot convert %s (%s) to %s (%s)" % (from_unit, dim_from, to_unit, dim_to))
    return value * f_from / f_to