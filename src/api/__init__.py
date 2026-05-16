"""
REST API Server for Context Broker

Fast, lightweight HTTP API using stdlib http.server.
Endpoints:
  GET  /health                    — health check
  GET  /stats                     — broker statistics
  POST /agents/register           — register an agent
  GET  /agents                    — list agents
  GET  /artifacts                 — list artifacts
  POST /artifacts/register        — register an artifact
  GET  /artifacts/{path}          — get artifact by path (URL-encoded)
  POST /changes/record            — record a change
  GET  /changes                   — list changes
  POST /changes/{id}/propagate    — propagate a change
  POST /propagate/pending         — propagate all pending changes
  POST /scan                      — scan a directory
  GET  /graph/blast-radius        — get blast radius for a file
  GET  /graph/dependents          — get dependents of a file
  GET  /detect/conflicts          — detect conflicts
  GET  /report                    — full project report
"""

import json
import re
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path

# Add parent to path so imports work when run as module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ..core import get_db, init_db
from ..graph import Graph
from ..detect import ChangeDetector
from ..propagate import Propagator


class BrokerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Context Broker API."""

    def log_message(self, format, *args):
        """Silent logging — suppress default stderr output."""
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path.rstrip("/"))
        params = parse_qs(parsed.query, keep_blank_values=True)
        # Flatten single-value params
        query = {k: v[0] if len(v) == 1 else v for k, v in params.items()}

        # Route
        if path == "/health":
            return self._send_json({"status": "ok", "service": "context-broker"})

        if path == "/stats":
            return self._send_json(self.graph.stats())

        if path == "/agents":
            active = query.get("active", "true").lower() == "true"
            return self._send_json({"agents": self.graph.list_agents(active)})

        if path == "/artifacts":
            team = query.get("team") or None
            atype = query.get("type") or None
            return self._send_json({
                "artifacts": [
                    {"id": a.id, "path": a.path, "type": a.artifact_type,
                     "owner": a.owner, "team": a.team}
                    for a in self.graph.list_artifacts(team, atype)
                ]
            })

        if path == "/changes":
            agent = query.get("agent") or None
            since = float(query.get("since", 0))
            unprop = query.get("unpropagated", "false").lower() == "true"
            changes = self.graph.get_changes(agent=agent, since=since,
                                              unpropagated_only=unprop)
            return self._send_json({
                "changes": [
                    {"id": c.id, "artifact_id": c.artifact_id, "agent": c.agent,
                     "type": c.change_type, "summary": c.summary,
                     "timestamp": c.timestamp, "propagated": c.propagated}
                    for c in changes
                ]
            })

        if path == "/graph/blast-radius":
            filepath = query.get("path")
            depth = int(query.get("depth", 5))
            if not filepath:
                return self._send_json({"error": "Missing 'path' parameter"}, 400)
            radius = self.graph.blast_radius(filepath, depth)
            return self._send_json({
                "artifact": filepath,
                "blast_radius": [
                    {"path": r["artifact"].path, "depth": r["depth"],
                     "type": r["dep_type"]}
                    for r in radius
                ],
                "affected_count": len(radius),
            })

        if path == "/graph/dependents":
            filepath = query.get("path")
            if not filepath:
                return self._send_json({"error": "Missing 'path' parameter"}, 400)
            deps = self.graph.get_dependents(filepath)
            return self._send_json({
                "artifact": filepath,
                "dependents": [
                    {"path": d["artifact"].path, "type": d["dep_type"],
                     "confidence": d["confidence"]}
                    for d in deps
                ],
            })

        if path == "/detect/conflicts":
            report = self.detector.detect_conflicting_decisions()
            return self._send_json({"conflicts": report})

        if path == "/report":
            root = query.get("root", ".")
            r = self.detector.full_report(root)
            return self._send_json(r)

        if path == "/propagate/stats":
            return self._send_json(self.propagator.propagation_stats())

        return self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path.rstrip("/"))
        body = self._read_body()

        if path == "/agents/register":
            name = body.get("name")
            if not name:
                return self._send_json({"error": "Missing 'name'"}, 400)
            self.graph.register_agent(name, body.get("team", ""),
                                       body.get("role", ""))
            return self._send_json({"status": "registered", "name": name})

        if path == "/artifacts/register":
            filepath = body.get("path")
            if not filepath:
                return self._send_json({"error": "Missing 'path'"}, 400)
            art = self.graph.register_artifact(
                path=filepath,
                artifact_type=body.get("type", "file"),
                owner=body.get("owner", ""),
                team=body.get("team", ""),
                description=body.get("description", ""),
                metadata=body.get("metadata", {}),
            )
            return self._send_json({"status": "registered", "id": art.id,
                                      "path": art.path})

        if path == "/changes/record":
            artifact_path = body.get("artifact")
            agent = body.get("agent")
            change_type = body.get("change_type", "modify")
            summary = body.get("summary", "")

            if not artifact_path or not agent:
                return self._send_json(
                    {"error": "Missing 'artifact' or 'agent'"}, 400)

            change = self.graph.record_change(
                artifact_path, agent, change_type, summary,
                body.get("details", {}))
            if change:
                return self._send_json({
                    "status": "recorded",
                    "change_id": change.id,
                    "artifact_id": change.artifact_id,
                })
            return self._send_json({"error": "Failed to record change"}, 500)

        if path == "/changes/propagate-all":
            results = self.propagator.propagate_all_pending()
            return self._send_json({
                "propagated": len(results),
                "results": [r.to_dict() for r in results],
            })

        if path == "/scan":
            root = body.get("root", ".")
            team = body.get("team", "")
            result = self.graph.scan_directory(root, team)
            return self._send_json(result)

        if path == "/subscribe":
            agent = body.get("agent")
            pattern = body.get("pattern")
            channel = body.get("channel", "webhook")
            if not agent or not pattern:
                return self._send_json(
                    {"error": "Missing 'agent' or 'pattern'"}, 400)
            self.graph.subscribe(agent, pattern, channel)
            return self._send_json({"status": "subscribed", "agent": agent,
                                      "pattern": pattern})

        return self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def run_server(host: str = "0.0.0.0", port: int = 7879):
    """Run the Context Broker HTTP server."""
    init_db()

    # Create shared instances attached to handler class
    BrokerHandler.graph = Graph()
    BrokerHandler.detector = ChangeDetector()
    BrokerHandler.propagator = Propagator()

    server = HTTPServer((host, port), BrokerHandler)
    print(f"Context Broker running on http://{host}:{port}")
    print(f"Endpoints:")
    print(f"  GET  /health")
    print(f"  GET  /stats")
    print(f"  POST /agents/register")
    print(f"  GET  /agents")
    print(f"  POST /artifacts/register")
    print(f"  POST /changes/record")
    print(f"  POST /changes/propagate-all")
    print(f"  POST /scan")
    print(f"  GET  /graph/blast-radius?path=<path>")
    print(f"  GET  /detect/conflicts")
    print(f"  GET  /report?root=<path>")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    port = int(os.environ.get("BROKER_PORT", "7879"))
    run_server(port=port)
