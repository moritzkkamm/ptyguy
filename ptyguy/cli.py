"""Command line: python -m ptyguy <command> ...

    python -m ptyguy check configs/dummy.yaml     # are all signals reachable?
"""
import argparse
import logging

from .signals import CATALOG, ConfigError, SignalMap, load_config

def cmd_check(args):
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(e)
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

def main(argv=None):
    p = argparse.ArgumentParser(prog="ptyguy")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    
    c = sub.add_parser("check", help="connect to every signal in a beamline config")
    c.add_argument("config")
    c.add_argument("--timeout", type=float, default=3.0, help="connect timeout, s")
    c.set_defaults(func=cmd_check)
    
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.CRITICAL,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)