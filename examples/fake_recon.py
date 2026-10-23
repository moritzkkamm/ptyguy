"""A stand-in reconstruction for testing the launcher. No physics.

    python examples/fake_recon.py path/to/job.json

Reads job.json, "iterates" (sleeps), writes progress.json and snapshots like a
real reconstruction with the PtyGuy hook would, then a result file.
Recipe params it understands:
    iterations   (default 20)
    seconds      total runtime (default 1.0)
    fail         exit with code 3 halfway (default false)
"""
import json
import os
import sys
import time
from pathlib import Path


def write_atomic(path, text):
    tmp = Path(path).with_name("." + Path(path).name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main(job_path):
    job = json.loads(Path(job_path).read_text())
    params, paths, snaps = job["params"], job["paths"], job["snapshots"]
    n = int(params.get("iterations", 20))
    dt = float(params.get("seconds", 1.0)) / n
    print("fake recon of %s, %d iterations, probe %s"
          % (job["event"]["file_path"], n, job["probe"]["path"]), flush=True)

    for it in range(1, n + 1):
        time.sleep(dt)
        error = 1.0 / it
        if it % snaps["every"] == 0 or it == n:
            write_atomic(Path(paths["snapshots"]) / ("iter_%05d.json" % it),
                         json.dumps({"iteration": it, "error": error}))
            old = sorted(Path(paths["snapshots"]).glob("iter_*.json"))[:-snaps["keep"]]
            for p in old:
                p.unlink()
        write_atomic(paths["progress"], json.dumps(
            {"iteration": it, "iterations": n, "error": error, "heartbeat": time.time()}))
        if params.get("fail") and it == n // 2:
            print("fake failure at iteration %d" % it, flush=True)
            return 3

    Path(paths["result"]).write_text("fake result for scan %d" % job["event"]["scan_number"])
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.environ["PTYGUY_JOB"]))
