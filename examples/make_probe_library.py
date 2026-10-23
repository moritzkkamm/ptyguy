"""Build a probe library (probes/*.npy + index.yaml) from the simulated datasets.

    python examples/make_probe_library.py sim/data probes

Uses the ground-truth probes stored in the simulated files. That's cheating
for a real reconstruction, but it gives the launcher a realistic library to
choose from. At a beamline the files would be calibrated probes instead.
"""
import json
import sys
from pathlib import Path

import h5py
import numpy as np
import yaml


def main(data_dir, out_dir):
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    entries = []
    for d in manifest["datasets"]:
        with h5py.File(data_dir / d["file"], "r") as f:
            probe = f["entry/ground_truth/probe"][()]
        name = "probe_%.2fum_%deV.npy" % (d["probe_size_um"], d["energy_eV"])
        np.save(out_dir / name, probe)
        entries.append({"file": name, "probe_size": d["probe_size_um"],
                        "energy": d["energy_eV"], "note": "ground truth from " + d["file"]})
        print("wrote", name)
    (out_dir / "index.yaml").write_text(yaml.safe_dump({"probes": entries}, sort_keys=False))
    print("wrote index.yaml")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
