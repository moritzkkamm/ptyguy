from ptyguy.listener import ScanEvent, SpoolSink
from ptyguy.spool import Spool


def event(n=1):
    return ScanEvent(uid="bl_%05d_x" % n, beamline="bl", status="complete", scan_number=n,
                     file_path="/d/f.h5", started=1.0, finished=2.0, emitted=3.0,
                     metadata={"energy": 700.0}, warnings=[])


def test_put_claim_finish(tmp_path):
    spool = Spool(tmp_path)
    SpoolSink(spool).emit(event(1))
    SpoolSink(spool).emit(event(2))
    assert spool.pending() == ["bl_00001_x.json", "bl_00002_x.json"]

    path = spool.claim("bl_00001_x.json")
    assert ScanEvent.from_dict(__import__("json").loads(path.read_text())) == event(1)
    assert spool.claim("bl_00001_x.json") is None        # second claimer loses
    spool.finish("bl_00001_x.json", ok=False, message="no recipe")
    assert (tmp_path / "failed" / "bl_00001_x.err").read_text() == "no recipe"
    assert spool.pending() == ["bl_00002_x.json"]


def test_temp_files_are_invisible(tmp_path):
    spool = Spool(tmp_path)
    (tmp_path / "incoming" / ".half_written.json.tmp").write_text("{")
    assert spool.pending() == []
