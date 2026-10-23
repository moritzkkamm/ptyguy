"""SignalMap against the running dummy beamline."""
import copy
import queue
import time
from pathlib import Path

import pytest

from ptyguy.cli import main as cli_main
from ptyguy.signals import SignalMap, load_config


@pytest.fixture
def sm(dummy):
    raw, _, _ = dummy
    m = SignalMap(load_config(raw))
    m.connect(timeout=10)
    yield m
    m.close()


def put(sm, prefix, suffix, value):
    """Write a raw PV the way a user at the beamline would (not via SignalMap)."""
    pv = sm._adapter._ctx.get_pvs(prefix + suffix)[0]
    pv.wait_for_connection(timeout=5)
    pv.write([value], wait=True)


def test_everything_connects(sm):
    assert all(err is None for err in sm.status.values()), sm.status
    assert sm.missing_required() == []


def test_read_is_logical_and_canonical(sm):
    assert sm.read("scan_state") == "idle"            # label mapped, not "IDLE" or 0
    assert sm.read("energy") == pytest.approx(700.0)
    assert sm.read("pixel_size") == pytest.approx(75.0)
    assert sm.read("scan_type") == "FERMAT"
    assert isinstance(sm.read("scan_number"), int)


def test_scan_events_and_snapshot(sm, dummy):
    prefix = dummy[2]
    events = queue.Queue()
    sm.subscribe("scan_state", events.put)
    put(sm, prefix, "SCAN:DURATION", 1.0)
    put(sm, prefix, "DET:FLUSH_DELAY", 0.5)
    put(sm, prefix, "SCAN:START", 1)

    seen = []
    deadline = time.time() + 10
    while "done" not in seen and time.time() < deadline:
        try:
            seen.append(events.get(timeout=0.5))
        except queue.Empty:
            pass
    assert seen[:1] == ["idle"]             # initial value on subscribe
    assert "running" in seen and seen[-1] == "done"

    snap = sm.snapshot()
    path = Path(snap["file_path"])
    assert path.is_absolute() and path.name == "scan_%05d.h5" % snap["scan_number"]
    assert snap["writing"] is True          # done, but file not flushed yet
    assert snap["probe_size"] == pytest.approx(2.952, rel=1e-3)


def test_unit_conversion_live(dummy):
    raw, _, prefix = dummy
    raw = copy.deepcopy(raw)
    raw["signals"]["energy"]["unit"] = "keV"        # pretend the IOC delivers keV
    m = SignalMap(load_config(raw))
    m.connect(timeout=10)
    try:
        assert m.read("energy") == pytest.approx(700_000.0)
    finally:
        m.close()


def test_busy_kind_emits_done(dummy):
    """Use DET:WRITING as a fake busy flag: 1 -> 0 must arrive as 'done'."""
    raw, _, prefix = dummy
    raw = copy.deepcopy(raw)
    raw["signals"]["scan_state"] = {"pv": prefix + "DET:WRITING", "kind": "busy"}
    m = SignalMap(load_config(raw))
    m.connect(timeout=10)
    events = queue.Queue()
    try:
        m.subscribe("scan_state", events.put)
        put(m, prefix, "SCAN:DURATION", 0.5)
        put(m, prefix, "DET:FLUSH_DELAY", 0.3)
        time.sleep(1.5)                      # let a previous scan's flush finish
        put(m, prefix, "SCAN:START", 1)
        seen = []
        deadline = time.time() + 10
        while "done" not in seen and time.time() < deadline:
            try:
                seen.append(events.get(timeout=0.5))
            except queue.Empty:
                pass
        assert "running" in seen and seen[-1] == "done"
    finally:
        m.close()


def test_check_cli_reports_missing_optional(dummy, tmp_path, capsys):
    raw, _, prefix = dummy
    raw = copy.deepcopy(raw)
    raw["signals"]["probe_size"]["pv"] = prefix + "OPT:DOES_NOT_EXIST"
    cfg_path = tmp_path / "broken.yaml"
    import yaml
    cfg_path.write_text(yaml.safe_dump(raw))
    code = cli_main(["check", str(cfg_path), "--timeout", "2"])
    out = capsys.readouterr().out
    assert code == 0                         # only an optional signal is broken
    assert "[--] probe_size" in out and "not reachable" in out
    assert "[ok] energy" in out
