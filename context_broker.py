#!/usr/bin/env python3
"""
Context Broker CLI

Usage:
    python -m context_broker --help
    python -m context_broker scan /path/to/project
    python -m context_broker report /path/to/project
    python -m context_broker server [--port 7879]
    python -m context_broker record --agent <name> --artifact <path> --type <type> --summary <text>
    python -m context_broker status
"""

import sys
import os
import json
import argparse

# Ensure src is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.graph import Graph
from src.detect import ChangeDetector
from src.propagate import Propagator
from src.api import run_server


def cmd_scan(args):
    g = Graph()
    result = g.scan_directory(args.path, args.team)
    print(json.dumps(result, indent=2))


def cmd_report(args):
    d = ChangeDetector()
    result = d.full_report(args.path)
    print(json.dumps(result, indent=2, default=str))


def cmd_record(args):
    g = Graph()
    g.register_agent(args.agent, args.team or "", args.role or "")
    change = g.record_change(args.artifact, args.agent, args.type, args.summary)
    if change:
        print(json.dumps({
            "change_id": change.id,
            "artifact": args.artifact,
            "agent": args.agent,
            "type": change.change_type,
            "timestamp": change.timestamp,
        }, indent=2))
    else:
        print("ERROR: Could not record change", file=sys.stderr)
        sys.exit(1)


def cmd_propagate(args):
    p = Propagator()
    results = p.propagate_all_pending()
    print(f"Propagated {len(results)} change(s):")
    for r in results:
        status = "OK" if r.success else "FAIL"
        print(f"  [{status}] {r.agent} via {r.channel}: {r.message}")


def cmd_status(args):
    g = Graph()
    stats = g.stats()
    print(json.dumps(stats, indent=2))


def cmd_blast(args):
    g = Graph()
    radius = g.blast_radius(args.path, args.depth)
    print(f"Blast radius for {args.path}: {len(radius)} affected artifacts")
    for r in radius:
        print(f"  [depth={r['depth']}] {r['artifact'].path} ({r['dep_type']})")


def cmd_server(args):
    run_server(port=args.port)


def main():
    parser = argparse.ArgumentParser(description="Context Broker CLI")
    sub = parser.add_subparsers(dest="command")

    # scan
    p_scan = sub.add_parser("scan", help="Scan directory and register artifacts")
    p_scan.add_argument("path", help="Root directory to scan")
    p_scan.add_argument("--team", default="", help="Team name")

    # report
    p_report = sub.add_parser("report", help="Full project conflict report")
    p_report.add_argument("path", help="Root directory")

    # record
    p_record = sub.add_parser("record", help="Record a change")
    p_record.add_argument("--agent", required=True, help="Agent name")
    p_record.add_argument("--artifact", required=True, help="Artifact path")
    p_record.add_argument("--type", default="modify", help="Change type")
    p_record.add_argument("--summary", default="", help="Change summary")
    p_record.add_argument("--team", default="", help="Agent team")
    p_record.add_argument("--role", default="", help="Agent role")

    # propagate
    sub.add_parser("propagate", help="Propagate all pending changes")

    # status
    sub.add_parser("status", help="Show broker statistics")

    # blast radius
    p_blast = sub.add_parser("blast", help="Calculate blast radius")
    p_blast.add_argument("path", help="Artifact path")
    p_blast.add_argument("--depth", type=int, default=5, help="Max depth")

    # server
    p_server = sub.add_parser("server", help="Start HTTP API server")
    p_server.add_argument("--port", type=int, default=7879, help="Port")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmds = {
        "scan": cmd_scan,
        "report": cmd_report,
        "record": cmd_record,
        "propagate": cmd_propagate,
        "status": cmd_status,
        "blast": cmd_blast,
        "server": cmd_server,
    }

    cmds[args.command](args)


if __name__ == "__main__":
    main()
