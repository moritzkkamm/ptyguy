"""launcher.yaml: what the user wants for this beamtime.

Same style as the beamline config: validate everything, report all problems
at once. Relative paths are relative to the launcher.yaml file itself, so a
config works no matter which folder you start PtyGuy from.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..listener.events import ABORTED, COMPLETE, INCOMPLETE
from ..signals.config import ConfigError

TOP_KEYS = {"spool", "analysis_root", "max_parallel", "reconstruct", "snapshots",
            "probes", "recipes"}
RECIPE_KEYS = {"name", "match", "run", "params", "probe"}
SNAPSHOT_DEFAULTS = {"every": 20, "keep": 3, "downsample": 1, "cleanup": "on_success"}
CLEANUP_CHOICES = ("on_success", "always", "never")
PROBE_DEFAULTS = {"index": None, "size_tolerance": 0.2, "energy_tolerance": 5.0}
_BRACE_HINT = ('hint: in YAML a value starting with { must be quoted, '
               'e.g.  run: "{python} my_recon.py {job}"')


@dataclass
class Recipe:
    name: str
    run: str                                   # command; {job} -> job.json, {python} -> this Python
    match: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    probe: str = "library"                     # "library" | "none" | a file path


@dataclass
class LauncherConfig:
    spool: Path
    analysis_root: Path
    max_parallel: int
    reconstruct: List[str]
    snapshots: Dict[str, Any]
    probes: Dict[str, Any]                     # index: Path or None, tolerances
    recipes: List[Recipe]
    base_dir: Path = Path(".")             # folder of launcher.yaml; jobs run here
    source: Optional[str] = None


def load_launcher_config(source):
    """Path to launcher.yaml, or a dict (then paths are relative to the cwd)."""
    if isinstance(source, dict):
        raw, base, origin = source, Path.cwd(), None
    else:
        path = Path(source).resolve()
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(["%s is not valid YAML: %s" % (path, e), _BRACE_HINT]) from None
        base, origin = path.parent, str(path)

    def resolve(p):
        p = Path(p)
        return p if p.is_absolute() else (base / p).resolve()

    problems = []
    unknown = set(raw) - TOP_KEYS
    if unknown:
        problems.append("unknown keys %s (known: %s)" % (sorted(unknown), ", ".join(sorted(TOP_KEYS))))

    max_parallel = raw.get("max_parallel", 1)
    if not isinstance(max_parallel, int) or max_parallel < 1:
        problems.append("max_parallel must be a whole number >= 1, got %r" % (max_parallel,))

    reconstruct = raw.get("reconstruct", [COMPLETE])
    if isinstance(reconstruct, str):
        reconstruct = [reconstruct]
    bad = set(reconstruct) - {COMPLETE, ABORTED, INCOMPLETE}
    if bad:
        problems.append("reconstruct: unknown status %s (use: complete, aborted, incomplete)"
                        % sorted(bad))

    snapshots = dict(SNAPSHOT_DEFAULTS)
    for key, value in (raw.get("snapshots") or {}).items():
        if key not in SNAPSHOT_DEFAULTS:
            problems.append("snapshots: unknown key %r (known: %s)"
                            % (key, ", ".join(SNAPSHOT_DEFAULTS)))
        snapshots[key] = value
    if snapshots["cleanup"] not in CLEANUP_CHOICES:
        problems.append("snapshots.cleanup must be one of %s" % ", ".join(CLEANUP_CHOICES))

    probes = dict(PROBE_DEFAULTS)
    for key, value in (raw.get("probes") or {}).items():
        if key not in PROBE_DEFAULTS:
            problems.append("probes: unknown key %r (known: %s)" % (key, ", ".join(PROBE_DEFAULTS)))
        probes[key] = value
    if probes["index"] is not None:
        probes["index"] = resolve(probes["index"])
        if not probes["index"].exists():
            problems.append("probes.index: file not found: %s" % probes["index"])

    recipes = []
    for i, entry in enumerate(raw.get("recipes") or []):
        label = "recipe %d (%s)" % (i + 1, entry.get("name", "no name"))
        n_before = len(problems)
        unknown = set(entry) - RECIPE_KEYS
        if unknown:
            problems.append("%s: unknown keys %s" % (label, sorted(unknown)))
        for key in ("name", "run"):
            if not entry.get(key):
                problems.append("%s: '%s' is missing" % (label, key))
        if entry.get("run") is not None and not isinstance(entry["run"], str):
            problems.append("%s: 'run' must be text. %s" % (label, _BRACE_HINT))
        elif entry.get("run") and "{job}" not in entry["run"]:
            problems.append("%s: 'run' must contain {job} (replaced by the path of job.json)"
                            % label)
        for key, rule in (entry.get("match") or {}).items():
            if isinstance(rule, list) and len(rule) == 2 and all(
                    isinstance(x, (int, float)) for x in rule) and rule[0] > rule[1]:
                problems.append("%s: match %s range %s has lo > hi" % (label, key, rule))
        probe = entry.get("probe", "library")
        if probe == "library" and probes["index"] is None:
            problems.append("%s: probe: library, but no probes.index is configured" % label)
        if probe not in ("library", "none"):
            probe = str(resolve(probe))
            if not Path(probe).exists():
                problems.append("%s: probe file not found: %s" % (label, probe))
        if len(problems) == n_before:
            recipes.append(Recipe(name=entry["name"], run=entry["run"],
                                  match=entry.get("match") or {},
                                  params=entry.get("params") or {}, probe=probe))
    if not raw.get("recipes"):
        problems.append("no recipes defined")

    if problems:
        raise ConfigError(problems)
    return LauncherConfig(
        spool=resolve(raw.get("spool", "spool")),
        analysis_root=resolve(raw.get("analysis_root", "analysis")),
        max_parallel=max_parallel, reconstruct=list(reconstruct), snapshots=snapshots,
        probes=probes, recipes=recipes, base_dir=base, source=origin)
