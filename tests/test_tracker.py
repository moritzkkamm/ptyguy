"""ScanTracker with scripted observations: no EPICS, no real files, no real time."""
import pytest

from ptyguy.listener import ABORTED, COMPLETE, INCOMPLETE, Obs, ScanTracker

OPTIONS = {"file_stable_s": 2.0, "file_timeout_s": 30.0}


class FakeFiles:
    """file_probe replacement: path -> size; missing = not there."""

    def __init__(self):
        self.sizes = {}

    def __call__(self, path):
        return (path in self.sizes, self.sizes.get(path, 0))


def snap(number, state="running", path=None, probe=1.5):
    return {"scan_state": state, "scan_number": number,
            "file_path": path if path is not None else "/d/scan_%05d.h5" % number,
            "writing": None, "energy": 700.0, "probe_size": probe}


class Script:
    """Feed observations in order, collect all events."""

    def __init__(self, has_writing=True):
        self.files = FakeFiles()
        self.tracker = ScanTracker("bl", OPTIONS, has_writing, file_probe=self.files)
        self.events = []

    def feed(self, kind, t, value=None, s=None, initial=False):
        self.events += self.tracker.feed(Obs(kind, t, value, snap=s, initial=initial))
        return self

    def ticks(self, t0, t1, dt=0.5):
        t = t0
        while t <= t1:
            self.feed("tick", t)
            t += dt
        return self


def normal_start(sc, number=1, t=0.0):
    sc.feed("state", t, "idle", snap(number - 1, "idle"), initial=True)
    sc.feed("writing", t + 0.1, False)
    sc.feed("writing", t + 1.0, True)
    sc.feed("state", t + 1.0, "running", snap(number))
    return sc


# ---------------------------------------------------------------------------
def test_complete_with_writing_flag():
    sc = normal_start(Script())
    sc.feed("state", 10, "done", snap(1, "done"))
    sc.ticks(10, 12)
    assert sc.events == []                       # done, but still writing
    sc.files.sizes["/d/scan_00001.h5"] = 3_500_000
    sc.feed("writing", 13, False)
    assert [e.status for e in sc.events] == [COMPLETE]
    ev = sc.events[0]
    assert ev.scan_number == 1 and ev.started == 1.0 and ev.finished == 10
    assert ev.metadata == {"energy": 700.0, "probe_size": 1.5} and ev.warnings == []
    assert ev.uid.startswith("bl_00001_")


def test_writing_drops_before_done_arrives():
    sc = normal_start(Script())
    sc.files.sizes["/d/scan_00001.h5"] = 100
    sc.feed("writing", 9.9, False)               # order swapped on the wire
    sc.feed("state", 10, "done", snap(1, "done"))
    assert [e.status for e in sc.events] == [COMPLETE]


def test_file_must_exist_even_if_writing_says_done():
    sc = normal_start(Script())
    sc.feed("state", 10, "done", snap(1, "done"))
    sc.feed("writing", 11, False)
    sc.ticks(11, 15)
    assert sc.events == []                       # no file yet (slow NFS)
    sc.files.sizes["/d/scan_00001.h5"] = 10
    sc.feed("tick", 16)
    assert [e.status for e in sc.events] == [COMPLETE]


def test_without_writing_flag_file_size_must_be_stable():
    sc = Script(has_writing=False)
    sc.feed("state", 0, "idle", snap(0, "idle"), initial=True)
    sc.feed("state", 1, "running", snap(1))
    sc.feed("state", 10, "done", snap(1, "done"))
    path = "/d/scan_00001.h5"
    for t, size in [(10.5, 0), (11, 100), (11.5, 200), (12, 300)]:  # still growing
        sc.files.sizes[path] = size
        sc.feed("tick", t)
    assert sc.events == []
    sc.ticks(12.5, 13.5)
    assert sc.events == []                       # stable only 1.5 s
    sc.feed("tick", 14.0)
    assert [e.status for e in sc.events] == [COMPLETE]


def test_abort_emits_immediately():
    sc = normal_start(Script())
    sc.feed("state", 5, "aborted", snap(1, "aborted"))
    assert [(e.status, e.finished) for e in sc.events] == [(ABORTED, 5)]
    assert sc.tracker.phase == "idle"


def test_timeout_gives_incomplete():
    sc = normal_start(Script())
    sc.feed("state", 10, "done", snap(1, "done"))
    sc.ticks(10, 39.5)
    assert sc.events == []
    sc.feed("tick", 40)
    assert [e.status for e in sc.events] == [INCOMPLETE]
    assert "no file at /d/scan_00001.h5 after 30 s" in sc.events[0].warnings


def test_file_path_refreshed_at_done():
    sc = normal_start(Script())
    sc.feed("state", 10, "done", snap(1, "done", path="/late/scan_1.h5"))
    sc.files.sizes["/late/scan_1.h5"] = 5
    sc.feed("writing", 11, False)
    assert sc.events[0].file_path == "/late/scan_1.h5"


def test_metadata_from_scan_start_not_end():
    sc = normal_start(Script())                  # probe 1.5 at start
    sc.feed("state", 10, "done", snap(1, "done", probe=9.9))
    sc.files.sizes["/d/scan_00001.h5"] = 5
    sc.feed("writing", 11, False)
    assert sc.events[0].metadata["probe_size"] == 1.5


def test_startup_states():
    sc = Script()
    sc.feed("state", 0, "done", snap(7, "done"), initial=True)
    sc.ticks(0, 40)
    assert sc.events == [] and sc.tracker.phase == "idle"     # can't know: ignore

    sc = Script()
    sc.feed("state", 0, "running", snap(7), initial=True)
    sc.feed("state", 5, "done", snap(7, "done"))
    sc.files.sizes["/d/scan_00007.h5"] = 5
    sc.feed("writing", 6, False)
    assert sc.events[0].scan_number == 7
    assert "listener started mid-scan" in sc.events[0].warnings[0]


def test_duplicate_states_after_reconnect_are_ignored():
    sc = normal_start(Script())
    sc.feed("connection", 3, False)
    sc.feed("connection", 4, True)
    sc.feed("state", 4.1, "running", snap(1))    # current value re-sent
    sc.feed("reconcile", 4.2, s=snap(1))
    assert sc.tracker.phase == "scanning" and sc.tracker.current.number == 1
    sc.feed("state", 10, "done", snap(1, "done"))
    sc.files.sizes["/d/scan_00001.h5"] = 5
    sc.feed("writing", 11, False)
    assert len(sc.events) == 1
    assert sc.events[0].warnings == ["connection lost during scan"]


def test_scan_ended_while_disconnected():
    sc = normal_start(Script())
    sc.feed("connection", 3, False)
    sc.feed("connection", 20, True)
    sc.feed("reconcile", 20, s=snap(1, "done"))
    assert sc.tracker.phase == "finishing"
    sc.files.sizes["/d/scan_00001.h5"] = 5
    sc.feed("writing", 20.1, False)
    assert [e.status for e in sc.events] == [COMPLETE]
    assert "scan ended while disconnected" in sc.events[0].warnings


def test_scans_missed_while_disconnected():
    sc = Script()
    sc.feed("state", 0, "idle", snap(4, "idle"), initial=True)
    sc.feed("connection", 1, False)
    sc.feed("connection", 60, True)
    sc.feed("reconcile", 60, s=snap(7, "idle"))  # 5 and 6 happened unseen, 7 is the latest
    sc.files.sizes["/d/scan_00007.h5"] = 5
    sc.feed("writing", 60.1, False)
    assert [e.scan_number for e in sc.events] == [7]
    ev = sc.events[0]
    assert ev.started is None and "scans 5, 6 missed entirely" in ev.warnings


def test_new_scan_while_previous_still_finishing():
    sc = normal_start(Script())
    sc.feed("state", 10, "done", snap(1, "done"))
    sc.feed("state", 11, "running", snap(2))     # scan 2 starts, scan 1 still writing
    assert not sc.tracker.finishing[0].trust_writing
    sc.files.sizes["/d/scan_00001.h5"] = 100
    sc.ticks(11.5, 13.5)                         # stable for 2 s -> scan 1 complete
    assert [(e.scan_number, e.status) for e in sc.events] == [(1, COMPLETE)]
    sc.feed("state", 20, "done", snap(2, "done"))
    sc.files.sizes["/d/scan_00002.h5"] = 100
    sc.feed("writing", 21, False)                # newest scan still uses the flag
    assert [(e.scan_number, e.status) for e in sc.events] == [(1, COMPLETE), (2, COMPLETE)]


def test_new_running_without_done_for_old_scan():
    sc = normal_start(Script())
    sc.feed("state", 10, "running", snap(2))     # 'done' of scan 1 lost somehow
    assert sc.tracker.current.number == 2
    assert sc.tracker.finishing[0].warnings == ["end of scan not observed"]


@pytest.mark.parametrize("bad", [None, {}])
def test_reconcile_without_data_is_harmless(bad):
    sc = normal_start(Script())
    sc.feed("reconcile", 5, s=bad)
    assert sc.tracker.phase == "scanning"
