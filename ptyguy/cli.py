"""Command line: python -m ptyguy <command> ...

    python -m ptyguy check  configs/dummy.yaml                 # are all signals reachable?
    python -m ptyguy listen configs/dummy.yaml --spool spool   # scan events -> spool/incoming
    python -m ptyguy launch examples/launcher.yaml             # spool -> reconstruction jobs
"""
import argparse
import logging
import os
import time

from .signals import CATALOG, ConfigError, SignalMap, load_config


def _load(path):
    """Config or None (problem already printed)."""
    try:
        return load_config(path)
    except FileNotFoundError:
        print("config file not found: %s (current folder: %s)" % (path, os.getcwd()))
    except ConfigError as e:
        print(e)
    return None


def cmd_check(args):
    config = _load(args.config)
    if config is None:
        return 2

    sm = SignalMap(config)
    print("beamline: %s (%s)  config: %s" % (config.beamline, config.control_system,
                                              args.config))
    sm.connect(timeout=args.timeout)
    rows = sm.check()
    width = max(len(name) for name, *_ in rows)
    for name, required, ok, text in rows:
        mark = "[ok]" if ok else ("[!!]" if required else "[--]")
        print("  %s %-*s  %s" % (mark, width, name, text))

    not_configured = [n for n in CATALOG if n not in config.signals]
    if not_configured:
        print("  not configured (optional): %s" % ", ".join(not_configured))

    missing = [name for name, required, ok, _ in rows if required and not ok]
    sm.close()
    if missing:
        print("\nrequired signals missing, PtyGuy cannot run here: %s" % ", ".join(missing))
        return 1
    print("\nall required signals ok")
    return 0


def cmd_listen(args):
    from .listener import ListenerService, SpoolSink
    from .spool import Spool

    config = _load(args.config)
    if config is None:
        return 2
    sm = SignalMap(config)
    sm.connect(timeout=args.timeout)
    missing = sm.missing_required()
    if missing:
        print("required signals not reachable: %s (try: ptyguy check %s)"
              % (", ".join(missing), args.config))
        sm.close()
        return 1

    spool = Spool(args.spool)
    service = ListenerService(sm, SpoolSink(spool))
    print("beamline %s, writing events to %s" % (config.beamline, spool.root / "incoming"))
    print("Ctrl+C to stop")
    service.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        sm.close()
    return 0


def cmd_launch(args):
    from .launcher import LauncherService, load_launcher_config

    try:
        config = load_launcher_config(args.config)
    except FileNotFoundError:
        print("config file not found: %s (current folder: %s)" % (args.config, os.getcwd()))
        return 2
    except ConfigError as e:
        print(e)
        return 2
    service = LauncherService(config)
    print("Ctrl+C to stop (running jobs keep running)")
    try:
        service.run_forever(poll_s=args.poll)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ptyguy")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="connect to every signal in a beamline config")
    c.add_argument("config")
    c.add_argument("--timeout", type=float, default=3.0, help="connect timeout, s")
    c.set_defaults(func=cmd_check, log_level=logging.CRITICAL)

    c = sub.add_parser("listen", help="watch the beamline, write one event per scan")
    c.add_argument("config")
    c.add_argument("--spool", default="spool", help="spool folder (default: ./spool)")
    c.add_argument("--timeout", type=float, default=5.0, help="connect timeout, s")
    c.set_defaults(func=cmd_listen, log_level=logging.INFO)

    c = sub.add_parser("launch", help="turn spooled scan events into reconstruction jobs")
    c.add_argument("config", help="launcher.yaml")
    c.add_argument("--poll", type=float, default=2.0, help="seconds between spool checks")
    c.set_defaults(func=cmd_launch, log_level=logging.INFO)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else args.log_level,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("caproto").setLevel(logging.CRITICAL)   # we report connections
    return args.func(args)
