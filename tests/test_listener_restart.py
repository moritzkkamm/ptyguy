"""The IOC dies while a file is being flushed and comes back: no scan may be lost.

Own module with its own IOC, because the test kills and restarts it.
"""
import time

import pytest
from caproto.threading.client import Context

from helpers import DummyIOC, put
from ptyguy.listener import COMPLETE, ListenerService, ListSink
from ptyguy.signals import SignalMap, load_config


@pytest.fixture
def restartable(tmp_path):
    d = DummyIOC(tmp_path)
    d.start()
    try:
        yield d
    finally:
        d.close()


def test_scan_survives_ioc_restart_during_flush(restartable):
    ioc = restartable
    sm = SignalMap(load_config(ioc.config()))
    sm.connect(timeout=10)
    sink = ListSink()
    service = ListenerService(sm, sink, tick_s=0.1)
    service.start()
    ctx = Context()
    try:
        put(ctx, ioc.prefix + "SCAN:DURATION", 0.5)
        put(ctx, ioc.prefix + "DET:FLUSH_DELAY", 30.0)  # file won't be flushed by this IOC
        put(ctx, ioc.prefix + "SCAN:START", 1)
        deadline = time.time() + 10
        while service.tracker.phase != "finishing" and time.time() < deadline:
            time.sleep(0.1)
        assert service.tracker.phase == "finishing"
        scan_file = service.tracker.finishing[0].file_path

        ioc.stop()                                     # IOC dies mid-flush
        time.sleep(2)
        assert not service.tracker.connected

        # the "file writer" finished on its own meanwhile (as a real one would)
        open(scan_file, "wb").write(b"\x89HDF\r\n\x1a\n" + b"x" * 100)
        ioc.start()                                    # fresh IOC: WRITING=0, NUMBER=0

        deadline = time.time() + 20
        while not sink.events and time.time() < deadline:
            time.sleep(0.1)
    finally:
        service.stop()
        sm.close()
        ctx.disconnect()

    assert [e.status for e in sink.events] == [COMPLETE]
    ev = sink.events[0]
    assert ev.scan_number == 1 and ev.file_path == scan_file
    assert "connection lost during scan" in ev.warnings
