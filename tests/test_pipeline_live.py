"""The whole chain: dummy beamline -> listener -> spool -> launcher -> reconstruction."""
import json
import subprocess
import sys
import time
from pathlib import Path

from caproto.threading.client import Context

from helpers import put
from ptyguy.launcher import DONE, LauncherService, load_launcher_config
from ptyguy.listener import ListenerService, SpoolSink
from ptyguy.signals import SignalMap, load_config
from ptyguy.spool import Spool

ROOT = Path(__file__).resolve().parent.parent


def test_scan_to_reconstruction(ioc, tmp_path):
    subprocess.run([sys.executable, str(ROOT / "examples" / "make_probe_library.py"),
                    str(ioc.tmp / "data"), str(tmp_path / "probes")], check=True,
                   capture_output=True)
    spool = Spool(tmp_path / "spool")
    launcher = LauncherService(load_launcher_config({
        "spool": str(spool.root), "analysis_root": str(tmp_path / "analysis"),
        "probes": {"index": str(tmp_path / "probes" / "index.yaml")},
        "recipes": [{"name": "dummy", "match": {"energy": [600, 800]},
                     "run": '{python} "%s" {job}' % (ROOT / "examples" / "fake_recon.py"),
                     "params": {"iterations": 10, "seconds": 0.5}}]}))

    sm = SignalMap(load_config(ioc.config()))
    sm.connect(timeout=10)
    listener = ListenerService(sm, SpoolSink(spool), tick_s=0.1)
    listener.start()
    ctx = Context()
    try:
        put(ctx, ioc.prefix + "SCAN:DURATION", 1.0)
        put(ctx, ioc.prefix + "DET:FLUSH_DELAY", 0.5)
        put(ctx, ioc.prefix + "SCAN:START", 1)

        deadline = time.time() + 30
        while time.time() < deadline:
            launcher.step()
            if list((spool.root / "done").glob("*.json")) and not launcher.busy:
                break
            time.sleep(0.1)
    finally:
        listener.stop()
        sm.close()
        ctx.disconnect()

    (job_dir,) = list((tmp_path / "analysis").iterdir())
    job = json.loads((job_dir / "job.json").read_text())
    status = json.loads((job_dir / "status.json").read_text())
    assert status["state"] == DONE
    assert job["event"]["status"] == "complete"
    assert Path(job["event"]["file_path"]).exists()
    # default probe 2.95 um -> the 2.0 um ground-truth probe is 32 % off: outside 20 %
    assert job["probe"]["path"] is None and "off" in job["probe"]["reason"]
