# PtyGuy

```
ptyguy/              the package
  signals/           signal mapping: logical signals <-> beamline PVs
    catalog.py       which logical signals exist, canonical units
    config.py        load + validate a beamline YAML, decode raw values
    epics.py         EPICS adapter (caproto client): connect/read/subscribe/close
    signalmap.py     SignalMap: the only thing the rest of PtyGuy uses
  cli.py             python -m ptyguy check <config>
configs/dummy.yaml   mapping for the dummy beamline
sim/                 dummy beamline (IOC + simulated data)
tests/               ptyguy tests (start their own dummy IOC)
```

```bat
pip install -e .[dev]
python -m pytest                                   rem all tests (ptyguy + sim)

rem live, with sim\dummy_beamline.py running:
set EPICS_CA_AUTO_ADDR_LIST=NO
set EPICS_CA_ADDR_LIST=127.0.0.1
python -m ptyguy check configs\dummy.yaml
```

Canonical units inside PtyGuy: energy in eV, lengths in um, distances in m.
