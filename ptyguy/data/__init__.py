"""Scan files: where things are (layout, from the beamline config) and reading them.

The reader (h5py, numpy) is imported lazily, so the listener on a control PC
can validate a config without h5py installed.
"""
from .layout import parse_data_layout  # noqa: F401


def __getattr__(name):                       # PEP 562: lazy module attributes
    if name in ("Scan", "load_scan", "read_scan"):
        from . import loader
        return getattr(loader, name)
    raise AttributeError(name)
