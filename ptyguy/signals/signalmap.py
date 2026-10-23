"""SignalMap: the only way the rest of PtyGuy talks to a beamline.

    sm = SignalMap.from_file("configs/dummy.yaml")
    sm.connect()
    sm.read("energy")                       # 700.0 (eV, whatever the beamline uses)
    sm.subscribe("scan_state", print)       # prints "running", "done", ...
    sm.snapshot()                           # all signals at once
"""
import logging
import threading

from .config import load_config

log = logging.getLogger("ptyguy.signals")

def make_adapter(control_system):
    if control_system == "epics":
        from .epics import EpicsAdapter
        return EpicsAdapter()
    raise ValueError("no adapter for control system %r" % control_system)

class _BusyEdge:
    """For 'kind: busy' scan states: turn the 1 -> 0 edge into 'done'.

    A busy flag only knows running/idle; PtyGuy wants a 'done' event.
    """

    def __init__(self, callback):
        self._callback = callback
        self._last = None
        self._lock = threading.Lock()

    def __call__(self, state):
        with self._lock:
            edge = self._last == "running" and state == "idle"
            self._last = state
        self._callback("done" if edge else state)
        
def _fmt(value):
    if isinstance(value, float):
        return "%.6g" % value
    if value == "":
        return "(empty)"
    return str(value)

class SignalMap:
    def __init__(self, config, adapter=None):
        self.config = config
        self._adapter = adapter
        self.status = {}             # signal name -> None (ok) or error message

    @classmethod
    def from_file(cls, path):
        return cls(load_config(path))

    # -- connection ---------------------------------------------------------
    def connect(self, timeout=5.0):
        """Connect every PV-backed signal. Returns {signal: None or error}."""
        if self._adapter is None:
            self._adapter = make_adapter(self.config.control_system)
        pv_of = {n: s.pv for n, s in self.config.signals.items() if not s.is_constant}
        raw_status = self._adapter.connect(list(pv_of.values()), timeout=timeout)
        self.status = {n: raw_status[pv] for n, pv in pv_of.items()}
        self.status.update({n: None for n, s in self.config.signals.items() if s.is_constant})
        for name, err in self.status.items():
            if err:
                level = logging.ERROR if self.config.signals[name].spec.required else logging.WARNING
                log.log(level, "%s: %s", name, err)
        return self.status
    
    def missing_required(self):
        return [n for n, err in self.status.items()
                if err and self.config.signals[n].spec.required]
        
    # access ----------------------------------------------------------
    def _signal(self, name):
        try:
            return self.config.signals[name]
        except KeyError:
            raise KeyError("signal %r is not configured for beamline %s"
                           % (name, self.config.beamline)) from None
            
    def has(self, name):
        return name in self.config.signals
    
    def read(self, name):
        sig = self._signal(name)
        raw = sig.value if sig.is_constant else self._adapter.read(sig.pv)
        return sig.decode(raw)
    
    def subscribe(self, name, callback):
        """callback(logical_value) on every chane (from bg thread)"""
        sig = self._signal(name)
        if sig.is_constant:
            raise ValueError("%s is a constant, nothing to subscribe to" % name)
        if sig.kind == "busy":
            callback = _BusyEdge(callback)
        self._adapter.subscribe(sig.pv, lambda raw: callback(sig.decode(raw)))
        
    def on_connection(self, callback):
        """callback(signal_name, connected: bool) when a signal's PV drops or returns.

        Call after connect(). Constants never disconnect, so they never report.
        """
        signals_of_pv = {}
        for name, sig in self.config.signals.items():
            if not sig.is_constant:
                signals_of_pv.setdefault(sig.pv, []).append(name)

        def on_pv(pvname, connected):
            for name in signals_of_pv.get(pvname, []):
                callback(name, connected)

        self._adapter.on_connection(on_pv)
        
    def snapshot(self):
        """Every configured signal -> calue; None where reading failed."""
        values = {}
        for name in self.config.signals:
            try:
                values[name] = self.read(name)
            except Exception as e:
                log.warning("snapshot: %s unreadable (%s)", name, e)
                values[name] = None
        return values
    
    def check(self):
        """One row per configured signal: (name, required, ok, text)."""
        rows = []
        for name, sig in self.config.signals.items():
            err = self.status.get(name)
            if err:
                rows.append((name, sig.spec.required, False, err))
                continue
            try:
                value = self.read(name)
                unit = (" " + sig.spec.unit) if sig.spec.unit else ""
                note = "  (constant)" if sig.is_constant else ""
                rows.append((name, sig.spec.required, True, "%s%s%s" % (_fmt(value), unit, note)))
            except Exception as e:
                rows.append((name, sig.spec.required, False, "read failed: %s" % e))
        return rows
    
    def close(self):
        if self._adapter is not None:
            self._adapter.close()