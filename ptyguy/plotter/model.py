"""What the plotter shows, read from the job folders. No matplotlib here.

The plotter only ever reads files the launcher and the hook write:
    status.json, progress.json, job.json, snapshots/iter_*.h5, result.h5
It must survive files disappearing under it (rolling snapshots get deleted),
so every read is allowed to fail; the last good data is kept.
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import h5py
import numpy as np

STALE_S = 30.0          # running, but no progress for this long: say so


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


@dataclass
class JobSummary:
    name: str
    dir: Path
    state: str
    queued: float


@dataclass
class Arrays:
    obj: np.ndarray
    probe: Optional[np.ndarray]
    error: np.ndarray
    iteration: int
    source: str                          # "snapshot iter_000045.h5" or "result.h5"


@dataclass
class JobView:
    summary: JobSummary
    status: Dict[str, Any]
    progress: Optional[Dict[str, Any]]
    job: Optional[Dict[str, Any]]
    arrays: Optional[Arrays]
    notes: List[str] = field(default_factory=list)       # warnings to show

    @property
    def scan_number(self):
        return (self.job or {}).get("event", {}).get("scan_number")


def list_jobs(analysis_root):
    """All job folders, newest first (by queue time)."""
    root = Path(analysis_root)
    jobs = []
    if not root.is_dir():
        return jobs
    for d in root.iterdir():
        status = _read_json(d / "status.json") if d.is_dir() else None
        if status:
            jobs.append(JobSummary(d.name, d, status.get("state", "?"),
                                   status.get("queued") or 0.0))
    return sorted(jobs, key=lambda j: j.queued, reverse=True)


def pick_follow(jobs):
    """The newest running job; if none runs, the newest job at all."""
    running = [j for j in jobs if j.state == "running"]
    return (running or jobs or [None])[0]


class JobReader:
    """Reads one job folder; re-reads arrays only when a newer file appeared."""

    def __init__(self, summary):
        self.summary = summary
        self.job = _read_json(summary.dir / "job.json")
        self._arrays = None
        self._arrays_key = None              # (file name, mtime) of what we showed last

    def read(self, now=None):
        now = now or time.time()
        d = self.summary.dir
        status = _read_json(d / "status.json") or {}
        progress = _read_json(d / "progress.json")
        self.summary.state = status.get("state", self.summary.state)

        candidate = self._newest_array_file()
        if candidate is not None:
            try:
                key = (candidate.name, candidate.stat().st_mtime)
                if key != self._arrays_key:
                    self._arrays = self._load(candidate)
                    self._arrays_key = key
            except (OSError, KeyError):
                pass                         # deleted / replaced meanwhile: next poll

        notes = list((self.job or {}).get("event", {}).get("warnings", []))
        if status.get("state") == "running" and progress and progress.get("heartbeat"):
            silent = now - progress["heartbeat"]
            if silent > STALE_S:
                notes.append("no progress for %.0f s (hung?)" % silent)
        if status.get("state") == "failed":
            notes.append(status.get("message", "failed"))
        return JobView(self.summary, status, progress, self.job, self._arrays, notes)

    def _newest_array_file(self):
        d = self.summary.dir
        if (d / "result.h5").exists():
            return d / "result.h5"
        snaps = sorted((d / "snapshots").glob("iter_*.h5")) if (d / "snapshots").is_dir() else []
        return snaps[-1] if snaps else None

    @staticmethod
    def _load(path):
        with h5py.File(path, "r") as f:
            obj = f["object"][()]
            probe = f["probe"][()] if "probe" in f else None
            error = f["error"][()] if "error" in f else np.zeros(0)
            iteration = int(f.attrs.get("iteration", f.attrs.get("iterations", len(error))))
        return Arrays(obj, probe, error, iteration,
                      "result.h5" if path.name == "result.h5" else "snapshot " + path.name)


# ---------------------------------------------------------------------------
# Display helpers (pure numpy, tested without matplotlib)
# ---------------------------------------------------------------------------
def display_phase(obj, wrapped=False):
    """Phase image and its symmetric colour limit.

    wrapped=False (default): remove the global phase offset (amplitude-weighted mean
    phase -> 0), then a linear range of +-max. Good for weak contrast.
    wrapped=True: raw phase, range fixed to +-pi (use a cyclic colormap).
    Never auto-scale a cyclic map to min/max: two-level data then looks uniform.
    """
    if wrapped:
        return np.angle(obj), np.pi
    amp = np.abs(obj)
    offset = np.angle(np.sum(obj * amp))
    phase = np.angle(obj * np.exp(-1j * offset))
    lim = float(np.percentile(np.abs(phase), 99.5)) or 1e-6
    return phase, lim
