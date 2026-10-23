# Dummy beamline

```bash
pip install caproto h5py numpy pytest
python simulate_data.py --out data          # once: 3 datasets + manifest.json

# Windows (cmd): set EPICS_CA_AUTO_ADDR_LIST=no & set EPICS_CA_ADDR_LIST=127.0.0.1
python dummy_beamline.py --datasets data --data-root scans --interfaces 127.0.0.1

python -m caproto.commandline.monitor DUMMY:SCAN:STATE DUMMY:DET:WRITING DUMMY:SCAN:PROGRESS
python -m caproto.commandline.put DUMMY:SCAN:START 1
python -m caproto.commandline.put DUMMY:SCAN:ABORT 1
python -m caproto.commandline.put DUMMY:OPT:ZP:Z 2.566          # 25 um defocus -> ~1.5 um probe
python -m caproto.commandline.get -d CHAR DUMMY:DET:FILEPATH            # long strings: read as CHAR

pytest tests
```