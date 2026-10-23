"""Probe library: pick a calibrated starting probe for a scan. Pure, except reading the index.

probes/index.yaml:
    probes:
      - file: zp150_defocus25um_700eV.npy    # relative to index.yaml
        probe_size: 1.50                     # um
        energy: 700                          # eV (optional: then it fits any energy)
        note: "calibrated on scan 4711"      # optional, ignored

PtyGuy never opens the probe files; it only passes the path on in job.json.
"""
from pathlib import Path

import yaml

from ..signals.config import ConfigError


class ProbeLibrary:
    def __init__(self, index_path, size_tolerance=0.2, energy_tolerance=5.0):
        self.index_path = Path(index_path)
        self.size_tolerance = float(size_tolerance)        # relative, 0.2 = 20 %
        self.energy_tolerance = float(energy_tolerance)    # absolute, eV
        raw = yaml.safe_load(self.index_path.read_text()) or {}
        self.entries, problems = [], []
        for i, e in enumerate(raw.get("probes") or []):
            if "file" not in e or "probe_size" not in e:
                problems.append("%s entry %d: needs 'file' and 'probe_size'" % (index_path, i + 1))
                continue
            path = (self.index_path.parent / e["file"]).resolve()
            if not path.exists():
                problems.append("%s entry %d: file not found: %s" % (index_path, i + 1, path))
                continue
            self.entries.append({"path": path, "probe_size": float(e["probe_size"]),
                                 "energy": None if e.get("energy") is None else float(e["energy"])})
        if problems:
            raise ConfigError(problems)

    def choose(self, probe_size, energy=None):
        """(path or None, reason). Nearest probe size among energy-compatible probes."""
        if probe_size is None:
            return None, "no probe_size in event"
        candidates = [e for e in self.entries
                      if e["energy"] is None or energy is None
                      or abs(e["energy"] - energy) <= self.energy_tolerance]
        if not candidates:
            return None, "no probe within %.0f eV of %s eV" % (self.energy_tolerance, energy)
        best = min(candidates, key=lambda e: abs(e["probe_size"] - probe_size))
        off = abs(best["probe_size"] - probe_size) / probe_size
        if off > self.size_tolerance:
            return None, ("nearest probe %.3g um is %.0f %% off %.3g um (tolerance %.0f %%)"
                          % (best["probe_size"], 100 * off, probe_size, 100 * self.size_tolerance))
        reason = "library: probe_size %.3g vs %.3g um (%.1f %% off)" % (
            best["probe_size"], probe_size, 100 * off)
        if best["energy"] is not None and energy is not None:
            reason += ", energy %.0f vs %.1f eV" % (best["energy"], energy)
        return str(best["path"]), reason
