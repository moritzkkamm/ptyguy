"""Load and validate a beamline config file; decode raw values into logical ones.

Everything here is pure Python: no control system involved, so all of it can
be tested without a running beamline.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .catalog import CATALOG, LOGICAL_STATES, UNITS, SignalSpec, convert, dimension

SUPPORTED_CONTROL_SYSTEMS = ("epics",)
SIGNAL_KEYS = {"pv", "value", "unit", "type", "states", "kind"}
DEFAULT_OPTIONS = {"file_stable_s": 2.0, "file_timeout_s": 300.0}
   
class ConfigError(Exception):
    """Raised with ALL problems at once, so a beamline setup is one fix round."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("invalid config:\n" + "\n".join("  - " + p for p in problems))
        
# Raw value helpers (what a control system hands us is messy)
def _to_number(raw):
    if hasattr(raw, "item"):           # numpy scalar -> plain Python
        raw = raw.item()
    if isinstance(raw, (bytes, str)):
        return float(_to_str(raw))
    return raw

def _to_str(raw):
    """bytes, char-waveform arrays (numpy uint8 / list of ints) or str -> str."""
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, bytes):
        text = raw.decode("latin-1")
    else:                              # array of character codes
        text = bytes(int(c) for c in raw).decode("latin-1")
    return text.rstrip("\x00").strip()


# One signal
@dataclass
class SignalConfig:
    name: str
    spec: SignalSpec
    pv: Optional[str] = None           # where to read it, or ...
    value: Any = None                  # ... a constant
    unit: Optional[str] = None         # unit the BEAMLINE delivers (default: canonical)
    type: Optional[str] = None         # "string": decode a char waveform
    states: Optional[Dict[str, List[Any]]] = None   # logical state -> beamline values
    kind: Optional[str] = None         # scan_state only: "busy" (1 = running)
    
    @property
    def is_constant(self):
        return self.pv is None
    
    def decode(self, raw):
        """Raw beamline value -> logical value in canonical units."""
        kind = self.spec.kind
        if kind == "state":
            return self._decode_state(raw)
        if kind == "str":
            return _to_str(raw)
        if kind == "bool":
            return self._decode_bool(raw)
        if kind == "int":
            return self._decode_int(raw)
        value = float(_to_number(raw))
        if self.spec.unit and self.unit:
            value = convert(value, self.unit, self.spec.unit)
        return value
    
    def _decode_state(self, raw):
        if self.kind == "busy":
            return "running" if _to_number(raw) else "idle"
        if isinstance(raw, (bytes, str)) or not hasattr(raw, "__len__"):
            label = _to_str(raw) if isinstance(raw, (bytes, str)) else _to_number(raw)
        else:
            label = raw
        for state, value in self.states.items():
            # compare as given and as string
            if any(label == v or str(label) == str(v) for v in value):
                return state
        return "other"
    
    def _decode_bool(self, raw):
        return bool(_to_number(raw))

    def _decode_int(self, raw):
        return int(_to_number(raw))
    
@dataclass
class BeamlineConfig:
    beamline: str
    control_system: str
    signals: Dict[str, SignalConfig]
    options: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_OPTIONS))
    data: Optional[Dict[str, Any]] = None      # layout of the scan files (ptyguy.data)
    source: Optional[str] = None
    
# loading and validation

def load_config(source):
    """Path to a YAML file or an already-parsed dict -> BeamlineConfig"""
    if isinstance(source, dict):
        raw, origin = source, None
    else:
        origin = str(source)
        raw = yaml.safe_load(Path(source).read_text()) or {}

    problems = []
    beamline = raw.get("beamline")
    if not beamline:
        problems.append("'beamline' is missing")

    control = raw.get("control_system", "epics")
    if control not in SUPPORTED_CONTROL_SYSTEMS:
        problems.append("control_system %r not supported (have: %s)"
                        % (control, ", ".join(SUPPORTED_CONTROL_SYSTEMS)))

    signals = {}
    for name, entry in (raw.get("signals") or {}).items():
        if name not in CATALOG:
            problems.append("unknown signal %r (known: %s)" % (name, ", ".join(CATALOG)))
            continue
        sig = _parse_signal(name, entry, problems)
        if sig is not None:
            signals[name] = sig

    for spec in CATALOG.values():
        if spec.required and spec.name not in signals:
            problems.append("required signal %r is missing" % spec.name)

    options = dict(DEFAULT_OPTIONS)
    for key, value in (raw.get("options") or {}).items():
        if key not in DEFAULT_OPTIONS:
            problems.append("unknown option %r (known: %s)" % (key, ", ".join(DEFAULT_OPTIONS)))
        else:
            options[key] = value
    from ..data.layout import parse_data_layout
    data = parse_data_layout(raw.get("data"), problems, signals=set(signals))
    if problems:
        raise ConfigError(problems)
    return BeamlineConfig(beamline, control, signals, options, data, origin)


def _parse_signal(name, entry, problems):
    spec = CATALOG[name]
    if not isinstance(entry, dict):
        problems.append("%s: expected a mapping like {pv: ...}, got %r" % (name, entry))
        return None

    n_before = len(problems)
    unknown = set(entry) - SIGNAL_KEYS
    if unknown:
        problems.append("%s: unknown keys %s" % (name, sorted(unknown)))

    has_pv, has_value = "pv" in entry, "value" in entry
    if has_pv == has_value:
        problems.append("%s: give exactly one of 'pv' or 'value'" % name)

    unit = entry.get("unit")
    if unit is not None:
        if spec.unit is None:
            problems.append("%s: has no unit, but unit %r was given" % (name, unit))
        elif unit not in UNITS and unit not in ("µm", "μm", "micron"):
            problems.append("%s: unknown unit %r (known: %s)" % (name, unit, ", ".join(UNITS)))
        elif dimension(unit) != dimension(spec.unit):
            problems.append("%s: unit %r is a %s, expected %s (like %r)"
                            % (name, unit, dimension(unit), dimension(spec.unit), spec.unit))

    sig_type = entry.get("type")
    if sig_type is not None and (sig_type != "string" or spec.kind != "str"):
        problems.append("%s: 'type: string' is the only type option, and only for text "
                        "signals" % name)

    states, kind = entry.get("states"), entry.get("kind")
    if spec.kind == "state":
        if (states is None) == (kind is None):
            problems.append("%s: give either 'states: {...}' or 'kind: busy'" % name)
        if kind is not None and kind != "busy":
            problems.append("%s: kind must be 'busy', got %r" % (name, kind))
        if states is not None:
            states = {k: v if isinstance(v, list) else [v] for k, v in states.items()}
            bad = set(states) - set(LOGICAL_STATES[:-1])
            if bad:
                problems.append("%s: unknown logical states %s (use: %s)"
                                % (name, sorted(bad), ", ".join(LOGICAL_STATES[:-1])))
            if "done" not in states:
                problems.append("%s: 'states' must say which value means 'done'" % name)
    elif states is not None or kind is not None:
        problems.append("%s: 'states' / 'kind' only apply to scan_state" % name)

    if len(problems) > n_before:
        return None
    return SignalConfig(name, spec, pv=entry.get("pv"), value=entry.get("value"),
                        unit=unit, type=sig_type, states=states, kind=kind)
    