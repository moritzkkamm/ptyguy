"""Client side: what PtyGuy's listener boils down to.

Subscribes to BUSY and, on every 1 -> 0 edge, reads the other PVs.
"""

import time
from caproto.threading.client import Context

ctx = Context()
busy, number, probe = ctx.get_pvs("DUMMY:SCAN:BUSY", "DUMMY:SCAN:NUMBER", "DUMMY:PROBE:SIZE")
last = {"v": None}

def on_busy(sub, response):
    v = response.data[0]
    if last["v"] == 1 and v == 0:
        n = number.read().data[0]
        p = probe.read().data[0]
        print("SCAN DONE: number=%d probe_size=%.2f um" % (n, p))
    last["v"] = v

busy.subscribe().add_callback(on_busy)
print("watching DUMMY:SCAN:BUSY ... (Ctrl+C to stop)")
while True:
    time.sleep(1)