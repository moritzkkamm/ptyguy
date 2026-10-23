"""The launcher loop: spool -> policy -> recipe -> probe -> job folder -> run -> follow.

step() does one pass and returns; run_forever() calls it every poll_s seconds.
Tests call step() directly, so there are no threads to wait for.
"""
import json
import logging
import time

from ..listener.events import ScanEvent
from ..spool import Spool
from .executors import LocalExecutor, render_command
from .jobs import DONE, FAILED, QUEUED, RUNNING, Job, new_job_dir, write_json_atomic
from .probes import ProbeLibrary
from .recipes import select

log = logging.getLogger("ptyguy.launcher")


class LauncherService:
    def __init__(self, config, executor=None, clock=time.time):
        self.config = config
        self.executor = executor or LocalExecutor()
        self.clock = clock
        self.spool = Spool(config.spool)
        p = config.probes
        self.probes = (ProbeLibrary(p["index"], p["size_tolerance"], p["energy_tolerance"])
                       if p["index"] else None)
        self.queued, self.running = [], []
        leftovers = list((self.spool.root / "claimed").glob("*.json"))
        if leftovers:
            log.warning("%d event(s) left in %s from an earlier run (launcher stopped while "
                        "they were running?). Move them back to incoming/ to re-run them: %s",
                        len(leftovers), leftovers[0].parent,
                        ", ".join(p.name for p in leftovers))

    # ------------------------------------------------------------------
    def step(self):
        """One pass: finish ended jobs, take new events, start what fits."""
        self._follow_running()
        for name in self.spool.pending():
            path = self.spool.claim(name)
            if path is None:
                continue
            try:
                self._intake(name, path)
            except Exception as e:                  # never leave an event stuck in claimed/
                log.exception("%s: intake failed", name)
                self.spool.finish(name, ok=False, message="intake failed: %r" % e)
        self._start_queued()

    def run_forever(self, poll_s=2.0, stop=lambda: False):
        log.info("launcher: spool %s, analysis %s, max %d parallel", self.spool.root,
                 self.config.analysis_root, self.config.max_parallel)
        while not stop():
            try:
                self.step()
            except Exception:
                log.exception("launcher step failed")   # keep running no matter what
            time.sleep(poll_s)

    @property
    def busy(self):
        return bool(self.queued or self.running)

    # -- intake ---------------------------------------------------------
    def _intake(self, name, path):
        try:
            event = ScanEvent.from_dict(json.loads(path.read_text()))
        except Exception as e:
            self.spool.finish(name, ok=False, message="unreadable event: %s" % e)
            log.error("%s: unreadable event (%s)", name, e)
            return

        if event.status not in self.config.reconstruct:
            msg = "skipped: status %s (reconstruct: %s)" % (event.status,
                                                            ", ".join(self.config.reconstruct))
            self.spool.finish(name, ok=True, message=msg)
            log.info("scan %d %s", event.scan_number, msg)
            return

        recipe, why = select(self.config.recipes, event)
        if recipe is None:
            self.spool.finish(name, ok=False, message="scan %d: %s" % (event.scan_number, why))
            log.error("scan %d: %s", event.scan_number, why)
            return

        probe_path, probe_reason = self._choose_probe(recipe, event)
        job_dir = new_job_dir(self.config.analysis_root, event.uid)
        job = Job(job_dir, name, recipe, command=None)
        job.command = render_command(recipe.run, job.job_json)
        job.snapshot_dir.mkdir()
        snaps = self.config.snapshots
        write_json_atomic(job.job_json, {
            "uid": job_dir.name,
            "event": event.to_dict(),
            "recipe": recipe.name,
            "params": recipe.params,
            "probe": {"path": probe_path, "reason": probe_reason},
            "snapshots": {"every": snaps["every"], "keep": snaps["keep"],
                          "downsample": snaps["downsample"]},
            "paths": {"job_dir": str(job_dir), "snapshots": str(job.snapshot_dir),
                      "progress": str(job_dir / "progress.json"),
                      "result": str(job_dir / "result.h5")},
            "created": self.clock(),
        })
        job.write_status(state=QUEUED, queued=self.clock())
        self.queued.append(job)
        log.info("scan %d -> recipe %s, probe: %s (%s) -> %s", event.scan_number, recipe.name,
                 probe_path or "none", probe_reason, job_dir.name)

    def _choose_probe(self, recipe, event):
        if recipe.probe == "none":
            return None, "recipe says none"
        if recipe.probe != "library":
            return recipe.probe, "fixed in recipe"
        return self.probes.choose(event.metadata.get("probe_size"), event.metadata.get("energy"))

    # -- running ----------------------------------------------------------
    def _start_queued(self):
        while self.queued and len(self.running) < self.config.max_parallel:
            job = self.queued.pop(0)
            try:
                job.handle = self.executor.start(job.command, self.config.base_dir,
                                                 job.dir / "job.log",
                                                 env={"PTYGUY_JOB": str(job.job_json)})
            except Exception as e:
                self._finish(job, FAILED, None, "could not start: %s" % e)
                continue
            job.write_status(state=RUNNING, started=self.clock(),
                             pid=self.executor.pid(job.handle))
            self.running.append(job)
            log.info("started %s: %s", job.dir.name, job.command)

    def _follow_running(self):
        for job in list(self.running):
            code = self.executor.poll(job.handle)
            if code is None:
                continue
            self.running.remove(job)
            if code == 0:
                self._finish(job, DONE, code, "ok")
            else:
                self._finish(job, FAILED, code, "exit code %d, see %s" % (code, job.dir / "job.log"))

    def _finish(self, job, state, code, message):
        job.write_status(state=state, ended=self.clock(), exit_code=code, message=message)
        cleanup = self.config.snapshots["cleanup"]
        if cleanup == "always" or (cleanup == "on_success" and state == DONE):
            job.cleanup_snapshots()
        self.spool.finish(job.spool_name, ok=(state == DONE),
                          message="%s: %s" % (job.dir.name, message))
        (log.info if state == DONE else log.error)("%s %s: %s", job.dir.name, state, message)
