"""Watch a beamline through PtyGuy's eyes.

Prints every change of scan_state and writing as PtyGuy sees it (logical
values, canonical units), plus a full snapshot whenever a scan ends.

    python examples/watch.py configs/dummy.yaml

Ctrl+C to stop.
"""
import sys
import threading
import time

from ptyguy.signals import SignalMap

t0 = time.time()
lock = threading.Lock()          # callbacks arrive from caproto's threads


def say(text):
    with lock:
        print("[%7.2f s] %s" % (time.time() - t0, text), flush=True)


def main(config_path):
    sm = SignalMap.from_file(config_path)
    status = sm.connect(timeout=5)
    missing = sm.missing_required()
    if missing:
        print("required signals not reachable: %s" % ", ".join(missing))
        print("is the dummy beamline running, and are the EPICS_CA_* variables set?")
        return 1
    say("connected to %s (%d signals)" % (sm.config.beamline, len(status)))

    def on_state(state):
        say("scan_state -> %s" % state)
        if state in ("done", "aborted"):
            # read in a separate thread: don't block caproto's callback thread
            threading.Thread(target=print_snapshot, args=(sm, state), daemon=True).start()

    def on_writing(writing):
        say("writing    -> %s" % writing)

    sm.subscribe("scan_state", on_state)
    if sm.has("writing"):
        sm.subscribe("writing", on_writing)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sm.close()
    return 0


def print_snapshot(sm, state):
    snap = sm.snapshot()
    lines = ["snapshot after '%s':" % state]
    for name, value in snap.items():
        unit = sm.config.signals[name].spec.unit or ""
        lines.append("    %-18s %s %s" % (name, value, unit))
    say("\n".join(lines))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))