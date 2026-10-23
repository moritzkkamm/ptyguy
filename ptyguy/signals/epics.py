"""EPICS adapter: raw PV access through caproto's threading client.

Only knows PV names and raw values. Translating into logical signals and
canonical units is SignalMap's job, so a Tango adapter only has to provide
the same four methods: connect, read, subscribe, close.
"""
import time

class EpicsAdapter:
    def __init__(self):
        from caproto import ChannelType
        from caproto.threading.client import Context
        self._ChannelType = ChannelType
        self._ctx = Context()
        self._pvs = {}
        self._callbacks = []   # caproto keeps only WEAK references to callbacks:
                               # without this list they are garbage collected and
                                # subscriptions silently stop firing
        self._conn_callbacks = []  # same reason, for connection callbacks
    # connection ---------------------------------------------------------
    def connect(self, pvnames, timeout=5.0):
        """Connect to all PVs. Returns {pvname: None if ok, else error message}."""
        new = [n for n in pvnames if n not in self._pvs]
        if new:
            for name, pv in zip(new, self._ctx.get_pvs(*new, timeout=timeout)):
                self._pvs[name] = pv
        status = {}
        deadline = time.monotonic() + timeout
        for name in pvnames:
            try:
                self._pvs[name].wait_for_connection(
                    timeout=max(0.1, deadline - time.monotonic()))
                status[name] = None
            except Exception:
                status[name] = "PV %s not reachable" % name
        return status
    
    # reading ------------------------------------------------------------
    def _request_type(self, pv):
        # Enums are requested as STRING so we get the label ("DONE"), not the
        # index (2). Everything else in its native type.
        if pv.channel.native_data_type == self._ChannelType.ENUM:
            return self._ChannelType.STRING
        return "native"
    
    def _unpack(self, pv, data):
        # char waveforms: keep the whole array (decoded to text later)
        if pv.channel.native_data_type == self._ChannelType.CHAR:
            return data
        if len(data) == 1:
            return data[0]
        return list(data)
    
    def read(self, pvname, timeout=2.0):
        pv = self._pvs[pvname]
        response = pv.read(data_type=self._request_type(pv), timeout=timeout)
        return self._unpack(pv, response.data)
    
    # monitoring ---------------------------------------------------------
    def subscribe(self, pvname, callback):
        """callback(raw_value) on every change (from a background thread)."""
        pv = self._pvs[pvname]
        sub = pv.subscribe(data_type=self._request_type(pv))
        
        def on_event(sub, response):
            callback(self._unpack(pv, response.data))
            
        sub.add_callback(on_event)
        self._callbacks.append((sub, on_event))   # keep a strong reference to avoid GC
        
    def on_connection(self, callback):
        """callback(pvname, connected: bool) whenever a connected PV drops or returns."""
        def on_state(pv, state):          # caproto passes "connected" / "disconnected"
            callback(pv.name, state == "connected")

        self._conn_callbacks.append(on_state)   # strong reference, see __init__
        for pv in self._pvs.values():
            pv.connection_state_callback.add_callback(on_state)

    def close(self):
        for sub, _ in self._callbacks:
            sub.clear()
        self._callbacks.clear()
        self._conn_callbacks.clear()            # <- new
        self._ctx.disconnect()