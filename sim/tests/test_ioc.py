"""Integration test: start the IOC as a subprocess and drive it like a user would."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
PREFIX = "TEST%d:" % os.getpid()        # unique prefix so parallel runs don't collide


@pytest.fixture(scope="module")
def ioc(tmp_path_factory):
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
    os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
    tmp = tmp_path_factory.mktemp("beamline")
    subprocess.run([sys.executable, str(HERE / "simulate_data.py"), "--out",
                    str(tmp / "data"), "--probe-sizes", "0.3", "1.5"], check=True,
                   capture_output=True)
    log_path = tmp / "ioc.log"
    log_file = open(log_path, "w")       # never PIPE without reading it: blocks on Windows
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "dummy_beamline.py"), "--datasets", str(tmp / "data"),
         "--data-root", str(tmp / "scans"), "--prefix", PREFIX, "--interfaces", "127.0.0.1"],
        stdout=log_file, stderr=subprocess.STDOUT)
    # try/finally: the IOC is shut down even if setup or a test crashes.
    # Otherwise it keeps running in the background and blocks port 5064.
    try:
        from caproto.threading.client import Context
        ctx = Context()
        names = ["OPT:ZP:Z", "OPT:ZP:MOVING", "OPT:PROBE:SIZE", "OPT:ENERGY", "SCAN:START",
                 "SCAN:ABORT", "SCAN:STATE", "SCAN:NUMBER", "SCAN:PROGRESS",
                 "SCAN:DURATION", "SCAN:NPOINTS", "DET:FILEPATH", "DET:WRITING",
                 "DET:FLUSH_DELAY"]
        pvs = dict(zip(names, ctx.get_pvs(*[PREFIX + n for n in names], timeout=5)))
        try:
            for pv in pvs.values():
                pv.wait_for_connection(timeout=10)
        except Exception:
            log_file.flush()
            state = "exited with %s" % proc.poll() if proc.poll() is not None else "running"
            pytest.fail("could not connect to the IOC (IOC %s). Is an old IOC still "
                        "holding port 5064? IOC log:\n%s" % (state, log_path.read_text()))
        yield pvs, tmp
    finally:
        proc.terminate()
        proc.wait(5)
        log_file.close()
        print("IOC log:", log_path)


def get(pv):
    from caproto import ChannelType
    if pv.name.endswith(":STATE"):           # enums: read the label, not the index
        return pv.read(data_type=ChannelType.STRING).data[0].decode()
    if pv.name.endswith(":FILEPATH"):        # char waveform: bytes -> str
        return bytes(pv.read().data).decode().rstrip("\x00")
    data = pv.read(data_type="native").data
    if isinstance(data, bytes):
        return data.decode().rstrip("\x00")
    v = data[0]
    return v.decode() if isinstance(v, bytes) else v


def wait_for(pv, value, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if get(pv) == value:
            return
        time.sleep(0.05)
    raise AssertionError("%s never became %r (is %r)" % (pv.name, value, get(pv)))


def test_zone_plate_move_changes_probe(ioc):
    pvs, _ = ioc
    before = get(pvs["OPT:PROBE:SIZE"])
    pvs["OPT:ZP:Z"].write([2.541 + 0.025], wait=True)      # 25 um defocus -> ~1.5 um
    wait_for(pvs["OPT:ZP:MOVING"], 1, timeout=2)
    wait_for(pvs["OPT:ZP:MOVING"], 0)
    assert get(pvs["OPT:PROBE:SIZE"]) < before               # closer to focus
    assert 1.2 < get(pvs["OPT:PROBE:SIZE"]) < 1.8           # -> 1.5 um dataset


def test_scan_done_before_file_ready(ioc):
    pvs, tmp = ioc
    pvs["SCAN:DURATION"].write([1.0], wait=True)
    pvs["SCAN:NPOINTS"].write([20], wait=True)
    pvs["DET:FLUSH_DELAY"].write([1.5], wait=True)
    pvs["SCAN:START"].write([1], wait=True)
    wait_for(pvs["SCAN:STATE"], "RUNNING", timeout=2)
    path = Path(get(pvs["DET:FILEPATH"]))
    assert path.name == "scan_00001.h5"
    wait_for(pvs["SCAN:STATE"], "DONE")
    # the race: scan is DONE but the file is not there yet
    assert get(pvs["DET:WRITING"]) == 1 and not path.exists()
    pvs["SCAN:START"].write([1], wait=True)                 # refused while writing
    time.sleep(0.3)
    assert get(pvs["SCAN:NUMBER"]) == 1
    wait_for(pvs["DET:WRITING"], 0)
    assert path.exists() and path.stat().st_size > 0
    assert (tmp / "data" / "sim_probe_1.50um.h5").samefile(path)   # hardlink, right probe


def test_abort_gives_empty_file(ioc):
    pvs, _ = ioc
    pvs["SCAN:DURATION"].write([5.0], wait=True)
    pvs["SCAN:START"].write([1], wait=True)
    wait_for(pvs["SCAN:STATE"], "RUNNING", timeout=2)
    time.sleep(0.5)
    pvs["SCAN:ABORT"].write([1], wait=True)
    wait_for(pvs["SCAN:STATE"], "ABORTED", timeout=2)
    wait_for(pvs["DET:WRITING"], 0, timeout=2)
    path = Path(get(pvs["DET:FILEPATH"]))
    assert path.name == "scan_00002.h5" and path.stat().st_size == 0
    assert 0 < get(pvs["SCAN:PROGRESS"]) < 200