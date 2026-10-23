"""ListenerService against the running dummy beamline."""
import json
import time

import pytest
from caproto.threading.client import Context

from helpers import put
from ptyguy.listener import ABORTED, COMPLETE, INCOMPLETE, ListenerService, SpoolSink
from ptyguy.signals import SignalMap, load_config
from ptyguy.spool import Spool


@pytest.fixture(scope="module")
def ctx():
    c = Context()
    yield c
    c.disconnect()


def listen(ioc, sink, **options):
    sm = SignalMap(load_config(ioc.config(**options)))
    sm.connect(timeout=10)
    service = ListenerService(sm, sink, tick_s=0.1)
    service.start()
    return sm, service


def wait_for_events(sink, n, timeout=15):
    deadline = time.time() + timeout
    while len(sink.events) < n and time.time() < deadline:
        time.sleep(0.1)
    return sink.events


class Collect:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)


def wait_idle(ioc, ctx):
    """Wait until the previous scan's file is flushed (START is refused otherwise)."""
    pv = ctx.get_pvs(ioc.prefix + "DET:WRITING")[0]
    pv.wait_for_connection(timeout=10)
    deadline = time.time() + 15
    while pv.read().data[0] != 0 and time.time() < deadline:
        time.sleep(0.1)


def test_complete_scan_lands_in_spool(ioc, ctx, tmp_path):
    spool = Spool(tmp_path / "spool")
    sink = SpoolSink(spool)
    collect = Collect()
    sink_both = type("Both", (), {"emit": lambda self, e: (sink.emit(e), collect.emit(e))})()
    sm, service = listen(ioc, sink_both)
    try:
        put(ctx, ioc.prefix + "SCAN:DURATION", 1.0)
        put(ctx, ioc.prefix + "DET:FLUSH_DELAY", 1.0)
        put(ctx, ioc.prefix + "SCAN:START", 1)
        events = wait_for_events(collect, 1)
    finally:
        service.stop()
        sm.close()

    assert [e.status for e in events] == [COMPLETE]
    ev = events[0]
    assert ev.warnings == []
    assert ev.emitted - ev.finished >= 0.9            # waited for the flush
    assert ev.metadata["probe_size"] == pytest.approx(2.952, rel=1e-3)
    assert ev.metadata["energy"] == pytest.approx(700.0)
    with open(ev.file_path, "rb") as f:                # the file really is there
        assert f.read(8).startswith(b"\x89HDF")
    (name,) = spool.pending()
    assert json.loads(spool.path("incoming", name).read_text())["uid"] == ev.uid


def test_abort(ioc, ctx):
    wait_idle(ioc, ctx)
    sink = Collect()
    sm, service = listen(ioc, sink)
    try:
        put(ctx, ioc.prefix + "SCAN:DURATION", 5.0)
        put(ctx, ioc.prefix + "SCAN:START", 1)
        time.sleep(1.0)
        put(ctx, ioc.prefix + "SCAN:ABORT", 1)
        events = wait_for_events(sink, 1)
    finally:
        service.stop()
        sm.close()
    assert [e.status for e in events] == [ABORTED]


def test_file_timeout_gives_incomplete(ioc, ctx):
    wait_idle(ioc, ctx)
    sink = Collect()
    sm, service = listen(ioc, sink, file_timeout_s=1.0)
    try:
        put(ctx, ioc.prefix + "SCAN:DURATION", 0.5)
        put(ctx, ioc.prefix + "DET:FLUSH_DELAY", 4.0)   # writer "hangs" longer than timeout
        put(ctx, ioc.prefix + "SCAN:START", 1)
        events = wait_for_events(sink, 1)
    finally:
        service.stop()
        sm.close()
        put(ctx, ioc.prefix + "DET:FLUSH_DELAY", 1.0)
    assert [e.status for e in events] == [INCOMPLETE]
    assert "after 1 s" in events[0].warnings[-1]
