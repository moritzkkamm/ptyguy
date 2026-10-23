"""Wiring: SignalMap callbacks -> queue -> one listener thread -> ScanTracker -> sink.

Callbacks (from caproto's threads) only put small observations into a queue.
All real work (reading snapshots, deciding, writing events) happens in one
thread, so the tracker never sees two things at once.
"""
import logging
import queue
import threading
import time

from .tracker import Obs, ScanTracker, probe_file

log = logging.getLogger("ptyguy.listener")


class ListenerService:
    def __init__(self, signalmap, sink, tick_s=0.5, clock=time.time, file_probe=probe_file):
        self.sm = signalmap
        self.sink = sink
        self.tick_s = tick_s
        self.clock = clock
        cfg = signalmap.config
        self.tracker = ScanTracker(cfg.beamline, cfg.options,
                                   has_writing=signalmap.has("writing"),
                                   file_probe=file_probe, data_layout=cfg.data)
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()          # guards the two fields below
        self._first_state = True
        self._lost = set()                     # signals currently disconnected

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        """Subscribe and start the worker thread. SignalMap must be connected."""
        self.sm.on_connection(self._on_connection)
        if self.sm.has("writing"):
            self.sm.subscribe("writing", self._on_writing)
        self.sm.subscribe("scan_state", self._on_state)
        self._thread = threading.Thread(target=self._run, name="ptyguy-listener",
                                        daemon=True)
        self._thread.start()
        log.info("listening on %s (writing flag: %s)", self.sm.config.beamline,
                 "yes" if self.sm.has("writing") else "no, using file size")

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # -- callbacks: caproto threads, keep them tiny ---------------------------
    def _on_state(self, state):
        with self._lock:
            initial, self._first_state = self._first_state, False
        self._queue.put(Obs("state", self.clock(), state, initial=initial))

    def _on_writing(self, writing):
        self._queue.put(Obs("writing", self.clock(), writing))

    def _on_connection(self, name, connected):
        """Many signals drop at once; report only 'first lost' and 'all back'."""
        with self._lock:
            was_ok = not self._lost
            (self._lost.discard if connected else self._lost.add)(name)
            now_ok = not self._lost
        if was_ok and not now_ok:
            self._queue.put(Obs("connection", self.clock(), False))
        elif now_ok and not was_ok:
            self._queue.put(Obs("connection", self.clock(), True))
            self._queue.put(Obs("reconcile", self.clock()))

    # -- worker thread ------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                obs = self._queue.get(timeout=self.tick_s)
            except queue.Empty:
                obs = Obs("tick", self.clock())

            if obs.kind == "connection":
                log.warning("connection %s", "restored" if obs.value else "LOST")
            if obs.kind in ("state", "reconcile") and obs.snap is None:
                obs.snap = self.sm.snapshot()  # here, not in the caproto thread

            try:
                events = self.tracker.feed(obs)
            except Exception:
                log.exception("tracker failed on %s", obs)
                continue
            for event in events:
                log.info(event.summary())
                try:
                    self.sink.emit(event)
                except Exception:
                    log.exception("could not emit %s", event.uid)
