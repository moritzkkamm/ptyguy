# PtyGuy

Automatic, live ptychography reconstructions at synchrotron beamlines.

PtyGuy watches the beamline control system (EPICS, Tango planned), notices when
a ptychography scan has finished **and** its data file is ready, picks the
right reconstruction recipe and starting probe, starts the reconstruction
(PtychoShelves, PtyPy, or any script), and shows its progress live.

```
 control PC                   shared filesystem                analysis side
 ──────────                   ─────────────────                ─────────────
 beamline PVs                                                  
   │                                                           
 SignalMap ─► Listener ─────► spool/incoming/*.json ─────────► Launcher
 (config)    "scan 12 done,   one event per scan                recipe + probe
              file ready"                                        │
                                                                 ▼
                              analysis/<scan>/                 your reconstruction
 Plotter  ◄────────────────── snapshots, progress, result ◄─── + ptyguy.hook
 (live view)
```

> **Status: v0.2, proof of concept.** The whole chain runs end to end against a
> simulated beamline (`sim/`) with a toy ePIE reconstruction. Not yet tested at
> a real beamline. Interfaces may still change.

## Why

At many beamlines, ptychography reconstructions are started by hand, after the
fact, with parameters copied from scan to scan. PtyGuy automates that loop
without touching the beamline's control system: it only **reads** PVs, and it
talks to the reconstruction side only through **files** on the filesystem the
data lands on anyway. No message broker, no open ports, works with offline
control PCs.

## Quick start (with the simulated beamline)

```bash
pip install -e .[dev]
python -m pytest                                        # ~85 tests, ~2 min

python sim/simulate_data.py --out sim/data              # 3 simulated scans (0.3, 1, 2 um probe)
python examples/make_probe_library.py sim/data probes   # starting probes for the launcher
```

Every terminal needs EPICS Channel Access restricted to this machine:

```bash
# Linux / macOS
export EPICS_CA_AUTO_ADDR_LIST=NO EPICS_CA_ADDR_LIST=127.0.0.1
```
```bat
:: Windows cmd (two lines: "set A=x & set B=y" would put a space into A)
set EPICS_CA_AUTO_ADDR_LIST=NO
set EPICS_CA_ADDR_LIST=127.0.0.1
```

Then, one terminal each:

```bash
python sim/dummy_beamline.py --datasets sim/data --data-root sim/scans --interfaces 127.0.0.1
python -m ptyguy listen configs/dummy.yaml --spool spool
python -m ptyguy launch examples/launcher.yaml
python -m ptyguy watch  examples/launcher.yaml
```

And play beamline operator:

```bash
python -m caproto.commandline.put DUMMY:OPT:ZP:Z 2.558     # zone plate -> ~1 um probe
python -m caproto.commandline.put DUMMY:SCAN:START 1       # scan -> reconstruction -> live plot
python -m caproto.commandline.put DUMMY:SCAN:ABORT 1       # aborted scans are skipped
```

## Commands

| Command | What it does |
|---|---|
| `ptyguy check <beamline.yaml>` | connect to every configured signal, show values in canonical units |
| `ptyguy check-data <beamline.yaml> <scan.h5>` | read a real scan file through the data mapping: shapes, positions, geometry, dx, overlap; lists the file's contents if a path is wrong |
| `ptyguy listen <beamline.yaml> --spool DIR` | watch the beamline, write one event per finished scan |
| `ptyguy launch <launcher.yaml>` | turn events into reconstruction jobs, run and follow them |
| `ptyguy watch <launcher.yaml>` | live plots: object phase/amplitude, probe, error; follows the newest running job (←/→ switch, `f` follow, `w` phase mode) |

All commands: `python -m ptyguy <command> --help`.

## Configuration: two files

**Beamline config** (`configs/dummy.yaml`) describes *what the beamline is*.
It is written once per beamline:

```yaml
beamline: dummy
control_system: epics
signals:                                   # PtyGuy's logical names -> this beamline's PVs
  scan_state:  {pv: DUMMY:SCAN:STATE, states: {running: RUNNING, done: DONE, aborted: ABORTED}}
  scan_number: {pv: DUMMY:SCAN:NUMBER}
  file_path:   {pv: DUMMY:DET:FILEPATH, type: string}
  writing:     {pv: DUMMY:DET:WRITING}     # optional: file still being written
  energy:      {pv: DUMMY:OPT:ENERGY, unit: eV}
  probe_size:  {pv: DUMMY:OPT:PROBE:SIZE, unit: um}
  # ...
data:                                      # where things are inside the scan files
  frames:    {path: entry/data/data}
  positions: {path: entry/data/positions_um, unit: um, order: yx}
  geometry:
    energy: {from: event, path: entry/instrument, attr: energy_eV, unit: eV}
```

Units are converted at the boundary; inside PtyGuy everything is eV, um and m.
A beamline with only a busy flag uses `scan_state: {pv: ..., kind: busy}`.

**Launcher config** (`examples/launcher.yaml`) describes *what you want this
beamtime*:

```yaml
spool: ../spool
analysis_root: ../analysis
max_parallel: 1
reconstruct: [complete]                    # skip aborted / incomplete scans
snapshots: {every: 5, keep: 3, cleanup: on_success}
probes: {index: ../probes/index.yaml, size_tolerance: 0.2, energy_tolerance: 5}
recipes:                                   # first match wins, any metadata field
  - name: small-probe
    match: {probe_size: [0, 1.2]}
    run: "{python} epie.py {job}"          # {job} -> job.json, {python} -> this Python
    params: {iterations: 100}
    probe: library                         # or a fixed file, or none
```

## Connecting your reconstruction

The launcher runs your command with the path of a `job.json` containing the
scan event, the recipe parameters, the chosen starting probe and where to put
results. Any language works; for Python there are two helpers:

```python
from ptyguy.data import load_scan
from ptyguy.hook import Hook, load_job

job  = load_job()                 # path from argv[1] or $PTYGUY_JOB
scan = load_scan(job)             # frames, positions_um (y, x), energy, distance, dx_um
hook = Hook(job)

for it in range(1, job["params"]["iterations"] + 1):
    obj, probe, error = my_engine_iteration(...)
    hook.update(it, error, obj=obj, probe=probe)   # cheap; writes snapshots when due
hook.finish(obj, probe)                            # result.h5
```

MATLAB scripts read `job.json` with `jsondecode(fileread(path))`. See
`examples/epie.py` for a complete example.

Each job lives in `analysis/<scan uid>/`:
`job.json` (input), `status.json` (launcher), `progress.json` and `snapshots/`
(hook), `job.log` (output), `result.h5`. Re-running a scan: copy its event from
`spool/done/` back into `spool/incoming/`; it gets a new folder (`..._r2`),
nothing is overwritten.

## Project layout

```
ptyguy/
  signals/     logical signals <-> PVs: catalog, config, EPICS adapter, SignalMap
  listener/    ScanTracker (pure state machine), events, service
  spool.py     the file queue between listener and launcher
  launcher/    config, recipes, probe library, job folders, executors, service
  data/        scan file layout + load_scan()
  hook.py      called from inside reconstructions: progress, snapshots, result
  plotter/     model (reads job folders) + matplotlib view
  cli.py
configs/       beamline configs
examples/      launcher.yaml, epie.py (toy ePIE), fake_recon.py, make_probe_library.py, watch.py
sim/           dummy beamline: caproto IOC + simulated ptychography data
tests/         pytest; live tests start their own dummy IOC
```

## Design rules

- **Decode at the boundary.** Beamline names, units and state words exist only
  in the config; the rest of PtyGuy sees logical values in canonical units.
- **Files, not sockets.** Listener, launcher, reconstruction and plotter only
  share files. Every file is written atomically (temp file + rename).
- **"Scan done" is not "file ready".** The listener waits for the detector's
  writing flag (or a stable file size) before it emits an event.
- **Raw data is read-only.** PtyGuy never writes to scan files; everything it
  produces goes into its own job folders.
- **Every bug found live gets a test.**

## Roadmap

- [ ] SLURM executor (same recipes, jobs on the cluster)
- [ ] MATLAB hook for PtychoShelves, PtyPy snapshot reader
- [ ] Tango adapter
- [ ] status back to the control system (PVs / Tango attributes)
- [ ] paired reconstructions (e.g. XMCD C+ / C-)
- [ ] first real beamline config
- [ ] setup GUI for beamline configs

## License

To be decided before the first public release (likely BSD-3-Clause).