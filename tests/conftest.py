"""Shared fixtures: a running dummy beamline with a unique PV prefix."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # import ptyguy without installing it

os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"

from helpers import DummyIOC  # noqa: E402


@pytest.fixture(scope="module")
def ioc(tmp_path_factory):
    """A running dummy beamline for one test module.

    scope="module": the IOC must stop before other test modules start their own.
    Two IOCs on one machine and one port: searches reach only one of them.
    """
    d = DummyIOC(tmp_path_factory.mktemp("dummy"))
    d.start()
    try:
        yield d
    finally:
        d.close()


@pytest.fixture(scope="module")
def dummy(ioc):
    """Backwards compatible: (config_dict, tmp_dir, prefix)."""
    return ioc.config(), ioc.tmp, ioc.prefix
