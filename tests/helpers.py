"""Test helpers (a normal module, so tests can import it; conftest.py can't be)."""
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "PTYT%d:" % os.getpid()


class DummyIOC:
    """A dummy beamline subprocess that a test can stop and restart."""

    def __init__(self, tmp, prefix=PREFIX):
        self.tmp, self.prefix, self.proc = Path(tmp), prefix, None
        subprocess.run([sys.executable, str(ROOT / "sim" / "simulate_data.py"), "--out",
                        str(self.tmp / "data"), "--probe-sizes", "0.3", "2.0"],
                       check=True, capture_output=True)
        self.log = open(self.tmp / "ioc.log", "a")

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "sim" / "dummy_beamline.py"), "--datasets",
             str(self.tmp / "data"), "--data-root", str(self.tmp / "scans"),
             "--prefix", self.prefix, "--interfaces", "127.0.0.1"],
            stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(5)

    def close(self):
        self.stop()
        self.log.close()

    def config(self, **options):
        """configs/dummy.yaml with our prefix, options overridden."""
        text = (ROOT / "configs" / "dummy.yaml").read_text().replace("DUMMY:", self.prefix)
        raw = yaml.safe_load(text)
        raw["options"].update(options)
        return raw


def put(ctx_or_sm, name, value):
    """Write a raw PV like a user would. Accepts a SignalMap or a caproto Context."""
    ctx = getattr(getattr(ctx_or_sm, "_adapter", None), "_ctx", ctx_or_sm)
    pv = ctx.get_pvs(name)[0]
    pv.wait_for_connection(timeout=10)
    pv.write([value], wait=True)
