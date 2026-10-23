"""What the listener produces: ScanEvent, and where it goes: sinks."""
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("ptyguy.listener")

COMPLETE, ABORTED, INCOMPLETE = "complete", "aborted", "incomplete"


@dataclass
class ScanEvent:
    uid: str                         # beamline + number + start time: unique, sortable
    beamline: str
    status: str                      # complete | aborted | incomplete
    scan_number: int
    file_path: str
    started: Optional[float]         # unix time (None if not observed)
    finished: Optional[float]        # scan_state reached done/aborted (None if not seen)
    emitted: float                   # when the listener decided
    metadata: Dict[str, Any] = field(default_factory=dict)   # canonical units
    warnings: List[str] = field(default_factory=list)
    data_layout: Optional[Dict[str, Any]] = None   # how to read file_path (beamline config `data:`)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    def summary(self):
        text = "scan %d %s: %s" % (self.scan_number, self.status.upper(), self.file_path)
        if self.warnings:
            text += "  [%s]" % "; ".join(self.warnings)
        return text


def make_uid(beamline, scan_number, started):
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(started))
    return "%s_%05d_%s" % (beamline, scan_number, stamp)


# ---------------------------------------------------------------------------
# Sinks: anything with emit(event)
# ---------------------------------------------------------------------------
class ListSink:
    """Collects events in memory (tests)."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class SpoolSink:
    """Writes each event as JSON into spool/incoming/ (atomically)."""

    def __init__(self, spool):
        self.spool = spool

    def emit(self, event):
        path = self.spool.put(event.to_json(), event.uid + ".json")
        log.info("spooled %s", path.name)
