"""ScanTracker: turns a stream of observations into exactly one event per scan.

Pure logic: no threads, no EPICS, no clock. Time comes in with every
observation, and the only outside access (does the file exist, how big is it)
goes through an injectable `file_probe`. That makes every edge case testable
with a scripted list of observations.

Observations (see Obs):
    state       scan_state changed            value = logical state, snap = all signals
    writing     writing flag changed          value = bool
    connection  connection lost / restored    value = bool
    reconcile   re-read after a reconnect     snap = all signals
    tick        time passes                   (checks files, timeouts)

Phases:
              running                done                  file ready
     IDLE ─────────────► SCANNING ───────────► FINISHING ─────────────► emit COMPLETE
                             │ aborted              │ timeout
                             └─► emit ABORTED       └─► emit INCOMPLETE

Several scans can be FINISHING at once (a new scan started while the previous
file is still being written). Only the newest one may trust the `writing`
flag; older ones fall back to "file size stable".
"""
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .events import ABORTED, COMPLETE, INCOMPLETE, ScanEvent, make_uid

# Signals that describe the scan itself; everything else in a snapshot is metadata.
_NOT_METADATA = ("scan_state", "scan_number", "file_path", "writing")


def probe_file(path):
    """(exists, size_in_bytes). The real filesystem; tests inject a fake."""
    try:
        return True, os.path.getsize(path)
    except OSError:
        return False, 0


@dataclass
class Obs:
    kind: str                                  # state | writing | connection | reconcile | tick
    t: float                                   # unix time of the observation
    value: Any = None
    snap: Optional[Dict[str, Any]] = None      # SignalMap.snapshot() for state / reconcile
    initial: bool = False                      # first value right after subscribing


@dataclass
class _Scan:
    number: int
    started: Optional[float]          # None: not known (happened while disconnected)
    file_path: str
    metadata: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    finished: Optional[float] = None
    trust_writing: bool = True                 # False once a newer scan has started
    last_size: int = -1
    size_since: Optional[float] = None


class ScanTracker:
    def __init__(self, beamline, options, has_writing, file_probe=probe_file):
        self.beamline = beamline
        self.stable_s = float(options.get("file_stable_s", 2.0))
        self.timeout_s = float(options.get("file_timeout_s", 300.0))
        self.has_writing = has_writing
        self.file_probe = file_probe

        self.current: Optional[_Scan] = None   # the scan in SCANNING, if any
        self.finishing: List[_Scan] = []       # scans waiting for their file
        self.writing: Optional[bool] = None    # last known writing flag
        self.connected = True
        self.last_number: Optional[int] = None

    @property
    def phase(self):
        if self.current is not None:
            return "scanning"
        return "finishing" if self.finishing else "idle"

    # ------------------------------------------------------------------
    def feed(self, obs):
        """Process one observation. Returns a list of ScanEvents (often empty)."""
        handler = getattr(self, "_on_" + obs.kind)
        events = handler(obs) or []
        return events + self._check_finishing(obs.t)

    # -- observation handlers -------------------------------------------
    def _on_state(self, obs):
        state, snap = obs.value, obs.snap or {}
        number = snap.get("scan_number")
        events = []

        if state == "running":
            if obs.initial:
                self._start(obs.t, snap, ["listener started mid-scan: start time and "
                                          "metadata read late"])
            elif self.current is None:
                self._start(obs.t, snap)
            elif number is not None and number != self.current.number:
                # a new scan is running, but we never saw the old one end
                self.current.warnings.append("end of scan not observed")
                self._to_finishing(self.current, obs.t, snap=None)
                self._start(obs.t, snap)
            # else: duplicate 'running' for the same scan, ignore

        elif state == "done":
            if self.current is not None and not obs.initial:
                self._to_finishing(self.current, obs.t, snap)
            # done without a scan we know about (startup, after reconnect): ignore,
            # reconcile handles scans that ended while we were disconnected

        elif state == "aborted":
            if self.current is not None and not obs.initial:
                scan, self.current = self.current, None
                scan.finished = obs.t
                events.append(self._event(scan, ABORTED, obs.t))

        # idle / other: no transition. (A busy-flag beamline reports 'done' itself.)
        if number is not None:
            self.last_number = number
        return events

    def _on_writing(self, obs):
        self.writing = bool(obs.value)

    def _on_connection(self, obs):
        self.connected = bool(obs.value)
        if not self.connected:
            for scan in self._open_scans():
                scan.warnings.append("connection lost during scan")

    def _on_reconcile(self, obs):
        """After a reconnect: compare what the beamline says now with what we knew."""
        snap = obs.snap or {}
        state, number = snap.get("scan_state"), snap.get("scan_number")
        if number is None:
            return []                          # still can't read it: nothing to do
        events = []
        cur = self.current

        if cur is not None:
            if number == cur.number:
                if state == "aborted":
                    self.current = None
                    cur.finished = obs.t
                    cur.warnings.append("abort seen only after reconnect")
                    events.append(self._event(cur, ABORTED, obs.t))
                elif state != "running":
                    cur.warnings.append("scan ended while disconnected")
                    self._to_finishing(cur, obs.t, snap)
                # still running: just carry on
            else:
                cur.warnings.append("scan ended while disconnected")
                self._to_finishing(cur, obs.t, snap=None)
                self._after_gap(number, state, snap, obs.t, last=cur.number)

        elif self.last_number is not None and number != self.last_number:
            self._after_gap(number, state, snap, obs.t, last=self.last_number)

        self.last_number = number
        return events

    def _on_tick(self, obs):
        pass                                   # _check_finishing runs after every obs

    # -- helpers ----------------------------------------------------------
    def _open_scans(self):
        return ([self.current] if self.current else []) + self.finishing

    def _start(self, t, snap, warnings=None):
        for older in self.finishing:           # the writing flag now belongs to the new scan
            older.trust_writing = False
        self.current = _Scan(
            number=snap.get("scan_number") if snap.get("scan_number") is not None else -1,
            started=t,
            file_path=snap.get("file_path") or "",
            metadata={k: v for k, v in snap.items() if k not in _NOT_METADATA},
            warnings=list(warnings or []))

    def _to_finishing(self, scan, t, snap):
        if snap and snap.get("file_path"):     # some beamlines set the path late
            scan.file_path = snap["file_path"]
        scan.finished = t
        if scan is self.current:
            self.current = None
        self.finishing.append(scan)
        
    def _after_gap(self, number, state, snap, t, last):
        """The scan number changed while we were disconnected."""
        if number > last:                       # scans happened while we were away
            self._note_new_scans(number, state, snap, t, after=last)
        elif state == "running":                # counter went DOWN: IOC restarted ...
            self._start(t, snap, ["scan counter reset from %d to %d (IOC restart?): "
                                  "start time and metadata read late" % (last, number)])
        # ... and nothing is running: no new scan, nothing to emit

    def _note_new_scans(self, number, state, snap, t, after):
        """Scans happened while we were disconnected; we only know the newest one."""
        missed = []
        if isinstance(after, int) and isinstance(number, int) and number > after + 1:
            missed = list(range(after + 1, number))
        warnings = ["happened while disconnected: metadata read after the fact"]
        if missed:
            warnings.append("scans %s missed entirely" % ", ".join(map(str, missed)))
        if state == "running":
            self._start(t, snap, warnings)
        elif state in ("done", "idle"):
            self._start(t, snap, warnings)
            self.current.started = None           # we don't know when it started
            self._to_finishing(self.current, t, snap)
        # aborted while disconnected: nothing to reconstruct, nothing to emit

    def _check_finishing(self, t):
        events = []
        for scan in list(self.finishing):
            status = self._file_status(scan, t)
            if status is None:
                continue
            self.finishing.remove(scan)
            events.append(self._event(scan, status, t))
        return events

    def _file_status(self, scan, t):
        """COMPLETE, INCOMPLETE, or None (keep waiting)."""
        exists, size = self.file_probe(scan.file_path) if scan.file_path else (False, 0)

        if self.has_writing and scan.trust_writing and self.connected:
            if exists and self.writing is False:
                return COMPLETE
        else:
            # no usable writing flag: file must exist and its size stop changing
            if size != scan.last_size:
                scan.last_size, scan.size_since = size, t
            elif exists and size > 0 and t - scan.size_since >= self.stable_s:
                return COMPLETE

        if t - scan.finished >= self.timeout_s:
            why = ("no file at %s" % scan.file_path) if not exists else "file never confirmed"
            scan.warnings.append("%s after %.0f s" % (why, self.timeout_s))
            return INCOMPLETE
        return None

    def _event(self, scan, status, t):
        started = scan.started if scan.started is not None else (scan.finished or t)
        return ScanEvent(
            uid=make_uid(self.beamline, max(scan.number, 0), started),
            beamline=self.beamline, status=status, scan_number=scan.number,
            file_path=scan.file_path, started=scan.started, finished=scan.finished,
            emitted=t, metadata=dict(scan.metadata), warnings=list(scan.warnings))
