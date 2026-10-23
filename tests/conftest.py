"""Shared fixture: a running dummy beamline with a unique PV prefix."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # import ptyguy without installing it
PREFIX = "PTYT%d:" % os.getpid()


# scope="module": the IOC must stop before other test modules start their own.
# Two IOCs on one machine and one port: searches reach only one of them.
@pytest.fixture(scope="module")
def dummy(tmp_path_factory):
    """Yields (config_dict, tmp_dir, prefix). Config = configs/dummy.yaml, our prefix."""
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
    os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
    tmp = tmp_path_factory.mktemp("dummy")
    subprocess.run([sys.executable, str(ROOT / "sim" / "simulate_data.py"), "--out",
                    str(tmp / "data"), "--probe-sizes", "0.3", "2.0"], check=True,
                   capture_output=True)
    log_path = tmp / "ioc.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "sim" / "dummy_beamline.py"), "--datasets",
         str(tmp / "data"), "--data-root", str(tmp / "scans"), "--prefix", PREFIX,
         "--interfaces", "127.0.0.1"], stdout=log_file, stderr=subprocess.STDOUT)
    try:
        text = (ROOT / "configs" / "dummy.yaml").read_text().replace("DUMMY:", PREFIX)
        yield yaml.safe_load(text), tmp, PREFIX
    finally:
        proc.terminate()
        proc.wait(5)
        log_file.close()
